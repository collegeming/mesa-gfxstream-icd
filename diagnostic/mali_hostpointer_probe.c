#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <unistd.h>

#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
#define RESULT(...) do { printf("RESULT hostpointer "); LOG(__VA_ARGS__); } while (0)
#define MAX_SPAN (64ULL * 1024 * 1024)

static bool has(const VkExtensionProperties *p,uint32_t n,const char *name) {
    for(uint32_t i=0;i<n;++i) if(!strcmp(p[i].extensionName,name)) return true;
    return false;
}
static void print_type(const VkPhysicalDeviceMemoryProperties *mp,uint32_t i) {
    if(i>=mp->memoryTypeCount) { LOG("TYPE index=%u ABSENT count=%u",i,mp->memoryTypeCount); return; }
    VkMemoryPropertyFlags f=mp->memoryTypes[i].propertyFlags;
    LOG("TYPE index=%u flags=0x%x heap=%u DEVICE_LOCAL=%u HOST_VISIBLE=%u HOST_COHERENT=%u HOST_CACHED=%u",
        i,f,mp->memoryTypes[i].heapIndex,!!(f&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT),
        !!(f&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT),!!(f&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT),
        !!(f&VK_MEMORY_PROPERTY_HOST_CACHED_BIT));
}
/* Reserve extra VA and overlay only our own reservation with a shared memfd
 * mapping. No pointer contents are read or written. Works with page sizes other
 * than 4096 and alignment larger than the page size. */
static void probe_pointer(VkDevice dev,const VkPhysicalDeviceMemoryProperties *mp,VkDeviceSize required) {
    PFN_vkGetMemoryHostPointerPropertiesEXT query=(PFN_vkGetMemoryHostPointerPropertiesEXT)
        vkGetDeviceProcAddr(dev,"vkGetMemoryHostPointerPropertiesEXT");
    if(!query) { RESULT("ERROR enabled extension missing vkGetMemoryHostPointerPropertiesEXT"); return; }
    long page_result=sysconf(_SC_PAGESIZE);
    if(page_result<=0 || !required || (required&(required-1))) {
        RESULT("BLOCKED invalid page/alignment page=%ld alignment=%" PRIu64,page_result,required); return;
    }
    uint64_t page=(uint64_t)page_result;
    if((page&(page-1)) || required>MAX_SPAN) { RESULT("BLOCKED bounded mapping limit alignment=%" PRIu64,required); return; }
    uint64_t alignment=required>page?required:page;
    uint64_t size=alignment>4096?alignment:4096;
    size=(size+alignment-1)/alignment*alignment;
    uint64_t span=size+alignment;
    if(span>MAX_SPAN || span>SIZE_MAX) { RESULT("BLOCKED bounded mapping span=%" PRIu64,span); return; }
    int fd=memfd_create("mali-hostpointer-no-gpu",MFD_CLOEXEC);
    if(fd<0) { RESULT("BLOCKED memfd_create errno=%d %s",errno,strerror(errno)); return; }
    void *reservation=MAP_FAILED;
    if(ftruncate(fd,(off_t)size)<0) { RESULT("BLOCKED ftruncate errno=%d %s",errno,strerror(errno)); goto done; }
    reservation=mmap(NULL,(size_t)span,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(reservation==MAP_FAILED) { RESULT("BLOCKED reserve VA errno=%d %s",errno,strerror(errno)); goto done; }
    uintptr_t base=(uintptr_t)reservation;
    if(base>UINTPTR_MAX-(uintptr_t)(alignment-1)) { RESULT("BLOCKED address overflow"); goto done; }
    uintptr_t aligned=(base+(uintptr_t)alignment-1)&~((uintptr_t)alignment-1);
    void *pointer=mmap((void *)aligned,(size_t)size,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_FIXED,fd,0);
    if(pointer==MAP_FAILED) { RESULT("BLOCKED MAP_SHARED overlay errno=%d %s",errno,strerror(errno)); goto done; }
    LOG("MAPPING fd=%d pointer=%p size=%" PRIu64 " page=%" PRIu64 " minAlignment=%" PRIu64 " pointerModulo=%" PRIuPTR " mode=memfd_MAP_SHARED untouched=YES",
        fd,pointer,size,page,required,(uintptr_t)pointer%(uintptr_t)required);
    VkMemoryHostPointerPropertiesEXT properties={.sType=VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT};
    VkResult r=query(dev,VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,pointer,&properties);
    LOG("QUERY vkGetMemoryHostPointerPropertiesEXT handle=HOST_ALLOCATION result=%d memoryTypeBits=0x%08x",r,properties.memoryTypeBits);
    if(r!=VK_SUCCESS) { RESULT("FAIL pointer-properties result=%d; no allocations attempted",r); goto done; }
    const uint32_t targets[]={8,14};
    for(unsigned t=0;t<2;++t) {
        uint32_t index=targets[t];
        if(index>=mp->memoryTypeCount || !(properties.memoryTypeBits&(1u<<index))) {
            RESULT("SKIP type=%u excluded by reported host-pointer mask=0x%08x or absent; no allocation",index,properties.memoryTypeBits);
            continue;
        }
        VkMemoryPropertyFlags flags=mp->memoryTypes[index].propertyFlags;
        if(mp->memoryTypes[index].heapIndex>=mp->memoryHeapCount ||
           size>mp->memoryHeaps[mp->memoryTypes[index].heapIndex].size) {
            RESULT("SKIP type=%u invalid/insufficient heap",index); continue;
        }
        VkImportMemoryHostPointerInfoEXT import={.sType=VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT,
            .handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,.pHostPointer=pointer};
        VkMemoryAllocateInfo ai={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.pNext=&import,
            .allocationSize=size,.memoryTypeIndex=index};
        VkDeviceMemory memory=VK_NULL_HANDLE;
        LOG("ALLOCATE type=%u flags=0x%x maskIncludes=YES size=%" PRIu64 " bind=NO mapVk=NO write=NO submit=NO",index,flags,size);
        r=vkAllocateMemory(dev,&ai,NULL,&memory);
        LOG("ALLOCATE type=%u vkAllocateMemory result=%d",index,r);
        if(r==VK_SUCCESS) {
            vkFreeMemory(dev,memory,NULL);
            RESULT("PASS type=%u HOST_VISIBLE=%u HOST_COHERENT=%u hostpointer import+free only; SystemBlob bind/GPU functionality NOT tested",
                index,!!(flags&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT),!!(flags&VK_MEMORY_PROPERTY_HOST_COHERENT_BIT));
        } else RESULT("FAIL type=%u hostpointer import result=%d; no bind/mapVk/submit",index,r);
    }
done:
    /* Imported memory was freed before its host-pointer backing is unmapped. */
    if(reservation!=MAP_FAILED) munmap(reservation,(size_t)span);
    close(fd);
}
int main(void) {
    setvbuf(stdout,NULL,_IONBF,0);
    struct utsname un; if(!uname(&un)) LOG("SYSTEM %s %s %s",un.sysname,un.release,un.machine);
    LOG("PROBE hostpointer-only-v1 commit=%s VulkanHeaders=%u AndroidAPI=%d",PROBE_REVISION,VK_HEADER_VERSION,__ANDROID_API__);
    LOG("SAFETY no get/use queue; no buffers/images/bind/mapVk/memory writes/shaders/commands/submissions; allocation may still invoke driver internals");
    uint32_t version=VK_API_VERSION_1_0;
    PFN_vkEnumerateInstanceVersion enumerate_version=(PFN_vkEnumerateInstanceVersion)vkGetInstanceProcAddr(NULL,"vkEnumerateInstanceVersion");
    if(enumerate_version) {
        VkResult r=enumerate_version(&version);
        if(r!=VK_SUCCESS) { RESULT("FAIL enumerate instance version=%d",r); return 1; }
    }
    LOG("LOADER version=%u.%u.%u",VK_API_VERSION_MAJOR(version),VK_API_VERSION_MINOR(version),VK_API_VERSION_PATCH(version));
    /* Vulkan 1.1 supplies EXT_external_memory_host's KHR_external_memory and
     * physical-device-properties2 dependencies; no unsupported extension calls. */
    if(version<VK_API_VERSION_1_1) { RESULT("UNSUPPORTED Vulkan 1.1 required"); return 2; }
    VkApplicationInfo app={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.pApplicationName="mali-hostpointer-only",.apiVersion=VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&app};
    VkInstance instance=VK_NULL_HANDLE;
    VkResult r=vkCreateInstance(&ici,NULL,&instance);
    if(r!=VK_SUCCESS) { RESULT("FAIL create instance=%d",r); return 1; }
    uint32_t count=0;
    r=vkEnumeratePhysicalDevices(instance,&count,NULL);
    if(r!=VK_SUCCESS || !count || count>16) { RESULT("FAIL physical devices result=%d count=%u",r,count); vkDestroyInstance(instance,NULL); return 1; }
    VkPhysicalDevice devices[16];
    r=vkEnumeratePhysicalDevices(instance,&count,devices);
    if(r!=VK_SUCCESS) { RESULT("FAIL physical devices result=%d",r); vkDestroyInstance(instance,NULL); return 1; }
    for(uint32_t d=0;d<count;++d) {
        VkPhysicalDevice pd=devices[d];
        VkPhysicalDeviceProperties p; vkGetPhysicalDeviceProperties(pd,&p);
        LOG("DEVICE index=%u name=%s vendor=0x%x driver=0x%x api=%u.%u.%u",d,p.deviceName,p.vendorID,p.driverVersion,
            VK_API_VERSION_MAJOR(p.apiVersion),VK_API_VERSION_MINOR(p.apiVersion),VK_API_VERSION_PATCH(p.apiVersion));
        VkPhysicalDeviceMemoryProperties mp; vkGetPhysicalDeviceMemoryProperties(pd,&mp);
        print_type(&mp,8); print_type(&mp,14);
        if(p.apiVersion<VK_API_VERSION_1_1) { RESULT("UNSUPPORTED device Vulkan 1.1 required"); continue; }
        uint32_t n=0;
        r=vkEnumerateDeviceExtensionProperties(pd,NULL,&n,NULL);
        if(r!=VK_SUCCESS || n>4096) { RESULT("FAIL extension count result=%d count=%u",r,n); continue; }
        VkExtensionProperties *ext=calloc(n?n:1,sizeof(*ext));
        if(!ext) { RESULT("FAIL extension allocation"); continue; }
        r=vkEnumerateDeviceExtensionProperties(pd,NULL,&n,ext);
        if(r!=VK_SUCCESS) { RESULT("FAIL enumerate extensions result=%d",r); free(ext); continue; }
        bool supported=has(ext,n,VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
        LOG("EXT VK_EXT_external_memory_host=%s VK_KHR_external_memory=%s VK_KHR_external_memory_fd=%s VK_EXT_external_memory_dma_buf=%s",
            supported?"YES":"NO",has(ext,n,VK_KHR_EXTERNAL_MEMORY_EXTENSION_NAME)?"YES":"NO",
            has(ext,n,VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME)?"YES":"NO",has(ext,n,VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME)?"YES":"NO");
        free(ext);
        if(!supported) { RESULT("UNSUPPORTED EXT_external_memory_host absent; no host-pointer query/allocation"); continue; }
        VkPhysicalDeviceExternalMemoryHostPropertiesEXT host={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT};
        VkPhysicalDeviceProperties2 p2={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,.pNext=&host};
        vkGetPhysicalDeviceProperties2(pd,&p2);
        LOG("HOST minImportedHostPointerAlignment=%" PRIu64,host.minImportedHostPointerAlignment);
        uint32_t nq=0; vkGetPhysicalDeviceQueueFamilyProperties(pd,&nq,NULL);
        if(!nq || nq>256) { RESULT("SKIP invalid queue-family count=%u",nq); continue; }
        VkQueueFamilyProperties *q=calloc(nq,sizeof(*q));
        if(!q) { RESULT("FAIL queue-family allocation"); continue; }
        vkGetPhysicalDeviceQueueFamilyProperties(pd,&nq,q);
        uint32_t family=0; while(family<nq && !q[family].queueCount) ++family;
        free(q);
        if(family==nq) { RESULT("SKIP no valid queue family for legal device creation"); continue; }
        float priority=1.0f;
        VkDeviceQueueCreateInfo qi={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=family,.queueCount=1,.pQueuePriorities=&priority};
        const char *enabled[]={VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME};
        VkDeviceCreateInfo dci={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&qi,
            .enabledExtensionCount=1,.ppEnabledExtensionNames=enabled};
        VkDevice dev=VK_NULL_HANDLE;
        LOG("CREATE_DEVICE queueCreateInfoCount=1 queueCount=1 family=%u enabled=VK_EXT_external_memory_host dependencies=Vulkan1.1-core; queue never retrieved/used",family);
        r=vkCreateDevice(pd,&dci,NULL,&dev);
        if(r!=VK_SUCCESS) { RESULT("FAIL create device=%d",r); continue; }
        probe_pointer(dev,&mp,host.minImportedHostPointerAlignment);
        vkDestroyDevice(dev,NULL);
    }
    vkDestroyInstance(instance,NULL);
    LOG("PROBE_DONE hostpointer-only; exit 0 means diagnostic completed, inspect per-type PASS/SKIP/FAIL; no GPU execution/performance claim");
    return 0;
}
