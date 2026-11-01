#define _GNU_SOURCE
#define VK_USE_PLATFORM_ANDROID_KHR
#include <vulkan/vulkan.h>
#include <android/hardware_buffer.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>

/* Linux UAPI, deliberately local: old NDK sysroots may lack linux/udmabuf.h. */
struct probe_udmabuf_create { uint32_t memfd, flags; uint64_t offset, size; };
#define PROBE_UDMABUF_CREATE _IOW('u', 0x42, struct probe_udmabuf_create)
struct probe_dma_buf_sync { uint64_t flags; };
#define PROBE_DMA_BUF_IOCTL_SYNC _IOW('b', 0, struct probe_dma_buf_sync)
#define PROBE_DMA_BUF_SYNC_READ (1ULL << 0)
#define PROBE_DMA_BUF_SYNC_WRITE (2ULL << 0)
#define PROBE_DMA_BUF_SYNC_END (1ULL << 2)
#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
#define RESULT(stage, ...) do { printf("RESULT stage=%s ", stage); LOG(__VA_ARGS__); } while (0)

static bool has(const VkExtensionProperties *p, uint32_t n, const char *s) {
    for (uint32_t i=0; i<n; ++i) if (!strcmp(p[i].extensionName,s)) return true;
    return false;
}
static int type_index(uint32_t bits, const VkPhysicalDeviceMemoryProperties *p) {
    for (uint32_t i=0; i<p->memoryTypeCount; ++i) if (bits & (1u<<i)) return (int)i;
    return -1;
}
static void buffer_caps(VkPhysicalDevice pd, VkExternalMemoryHandleTypeFlagBits h) {
    VkPhysicalDeviceExternalBufferInfo in = {.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO,
        .usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT, .handleType=h};
    VkExternalBufferProperties out = {.sType=VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES};
    vkGetPhysicalDeviceExternalBufferProperties(pd,&in,&out);
    VkExternalMemoryProperties p=out.externalMemoryProperties;
    LOG("CAP buffer handle=0x%x usage=TRANSFER_SRC|TRANSFER_DST features=0x%x importable=%u exportable=%u dedicated=%u compatible=0x%x exportFromImported=0x%x",
        h,p.externalMemoryFeatures,!!(p.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT),
        !!(p.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT),
        !!(p.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT),p.compatibleHandleTypes,p.exportFromImportedHandleTypes);
}
static void image_caps(VkPhysicalDevice pd, VkExternalMemoryHandleTypeFlagBits h) {
    VkFormat formats[]={VK_FORMAT_R8G8B8A8_UNORM,VK_FORMAT_B8G8R8A8_UNORM};
    VkImageUsageFlags usages[]={VK_IMAGE_USAGE_SAMPLED_BIT,VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,
        VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT};
    for (unsigned f=0;f<2;++f) for(unsigned t=0;t<2;++t) for(unsigned u=0;u<3;++u) {
        VkPhysicalDeviceExternalImageFormatInfo ext={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,.handleType=h};
        VkPhysicalDeviceImageFormatInfo2 in={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2,.pNext=&ext,
            .format=formats[f],.type=VK_IMAGE_TYPE_2D,.tiling=t?VK_IMAGE_TILING_LINEAR:VK_IMAGE_TILING_OPTIMAL,.usage=usages[u]};
        VkExternalImageFormatProperties external={.sType=VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES};
        VkImageFormatProperties2 out={.sType=VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2,.pNext=&external};
        VkResult r=vkGetPhysicalDeviceImageFormatProperties2(pd,&in,&out);
        VkExternalMemoryProperties p=external.externalMemoryProperties;
        LOG("CAP image format=%s tiling=%s usage=0x%x handle=0x%x result=%d features=0x%x importable=%u dedicated=%u compatible=0x%x",
            f?"BGRA8":"RGBA8",t?"LINEAR":"OPTIMAL",usages[u],h,r,p.externalMemoryFeatures,
            r==VK_SUCCESS && !!(p.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT),
            !!(p.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT),p.compatibleHandleTypes);
    }
}
static int dma_cpu_sync(int fd, uint64_t flags, const char *phase) {
    struct probe_dma_buf_sync sync={.flags=flags};
    int r;
    /* No indefinite retry if the device is interrupted or busy. */
    for(unsigned attempt=0;;++attempt) {
        r=ioctl(fd,PROBE_DMA_BUF_IOCTL_SYNC,&sync);
        if(r==0 || (errno!=EINTR && errno!=EAGAIN) || attempt==7) break;
    }
    if(r<0) LOG("DMA CPU sync phase=%s flags=0x%" PRIx64 " errno=%d %s",phase,flags,errno,strerror(errno));
    else LOG("DMA CPU sync phase=%s flags=0x%" PRIx64 " OK",phase,flags);
    return r;
}
static uint64_t monotonic_ns(void) {
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t)) return 0;
    return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
}
static void dma_gpu_fill(VkDevice dev,uint32_t family,VkBuffer buffer,int cpu_fd) {
    const uint32_t pattern=0x1234abcd;
    const size_t bytes=4096;
    void *mapping=MAP_FAILED;
    VkCommandPool pool=VK_NULL_HANDLE; VkFence fence=VK_NULL_HANDLE;
    VkResult r;
    mapping=mmap(NULL,bytes,PROT_READ|PROT_WRITE,MAP_SHARED,cpu_fd,0);
    if(mapping==MAP_FAILED) { RESULT("dmabuf-gpu","BLOCKED mmap errno=%d %s",errno,strerror(errno)); return; }
    /* This is a dma-buf mmap, NOT vkMapMemory. DMA_BUF_IOCTL_SYNC performs the
     * CPU cache flush/invalidate; vkFlush/InvalidateMappedMemoryRanges would be
     * invalid here. Initialize a different sentinel so stale data cannot PASS. */
    if(dma_cpu_sync(cpu_fd,PROBE_DMA_BUF_SYNC_WRITE,"START_WRITE")<0) {
        RESULT("dmabuf-gpu","BLOCKED CPU write sync; no GPU submit"); goto done;
    }
    memset(mapping,0xa5,bytes);
    if(dma_cpu_sync(cpu_fd,PROBE_DMA_BUF_SYNC_WRITE|PROBE_DMA_BUF_SYNC_END,"END_WRITE")<0) {
        RESULT("dmabuf-gpu","BLOCKED CPU write flush; no GPU submit"); goto done;
    }
    VkCommandPoolCreateInfo pci={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags=VK_COMMAND_POOL_CREATE_TRANSIENT_BIT,.queueFamilyIndex=family};
    r=vkCreateCommandPool(dev,&pci,NULL,&pool);
    if(r!=VK_SUCCESS) { RESULT("dmabuf-gpu","FAIL create-pool result=%d",r); goto done; }
    VkCommandBufferAllocateInfo cai={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool=pool,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
    VkCommandBuffer cmd=VK_NULL_HANDLE;
    r=vkAllocateCommandBuffers(dev,&cai,&cmd);
    if(r!=VK_SUCCESS) { RESULT("dmabuf-gpu","FAIL allocate-command result=%d",r); goto done; }
    VkCommandBufferBeginInfo begin={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    r=vkBeginCommandBuffer(cmd,&begin);
    if(r!=VK_SUCCESS) { RESULT("dmabuf-gpu","FAIL begin-command result=%d",r); goto done; }
    /* Fresh VkBuffer, first and sole Vulkan queue, no foreign GPU/user. CPU mmap
     * is host access, not a FOREIGN queue owner: no ownership transfer needed. */
    VkBufferMemoryBarrier before={.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
        .srcAccessMask=VK_ACCESS_HOST_WRITE_BIT,.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT,
        .srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,
        .buffer=buffer,.offset=0,.size=bytes};
    vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,NULL,1,&before,0,NULL);
    vkCmdFillBuffer(cmd,buffer,0,bytes,pattern);
    VkBufferMemoryBarrier after={.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
        .srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT,.dstAccessMask=VK_ACCESS_HOST_READ_BIT,
        .srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,
        .buffer=buffer,.offset=0,.size=bytes};
    vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,0,NULL,1,&after,0,NULL);
    r=vkEndCommandBuffer(cmd);
    if(r!=VK_SUCCESS) { RESULT("dmabuf-gpu","FAIL end-command result=%d",r); goto done; }
    VkFenceCreateInfo fci={.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    r=vkCreateFence(dev,&fci,NULL,&fence);
    if(r!=VK_SUCCESS) { RESULT("dmabuf-gpu","FAIL create-fence result=%d",r); goto done; }
    VkQueue queue=VK_NULL_HANDLE; vkGetDeviceQueue(dev,family,0,&queue);
    VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cmd};
    LOG("DMA GPU vkCmdFillBuffer bytes=%zu pattern=0x%08" PRIx32 " family=%u ownership=IGNORED fenceTimeoutNs=5000000000",bytes,pattern,family);
    uint64_t start=monotonic_ns();
    r=vkQueueSubmit(queue,1,&submit,fence);
    LOG("DMA GPU vkQueueSubmit result=%d",r);
    if(r!=VK_SUCCESS) {
        RESULT("dmabuf-gpu","FAIL submit result=%d; process exits without unsafe GPU cleanup",r);
        _Exit(3);
    }
    r=vkWaitForFences(dev,1,&fence,VK_TRUE,5000000000ULL);
    uint64_t end=monotonic_ns();
    LOG("DMA GPU vkWaitForFences result=%d submitToFenceHostNs=%" PRIu64 " (includes CPU/driver overhead, NOT a throughput benchmark)",r,end>=start?end-start:0);
    if(r!=VK_SUCCESS) {
        RESULT("dmabuf-gpu","%s fence result=%d; no readback/no queue-idle; exit without destroying pending objects",r==VK_TIMEOUT?"TIMEOUT":"FAIL",r);
        /* Do not free an in-flight buffer/pool/fence or wait indefinitely in
         * vkDeviceWaitIdle/vkDestroyDevice. OS teardown on process exit. */
        _Exit(r==VK_TIMEOUT?4:3);
    }
    if(dma_cpu_sync(cpu_fd,PROBE_DMA_BUF_SYNC_READ,"START_READ")<0) {
        RESULT("dmabuf-gpu","BLOCKED read invalidate; fence completed, data not verified"); goto done;
    }
    const volatile uint32_t *words=(const volatile uint32_t *)mapping;
    size_t mismatches=0,first=0;
    uint32_t first_value=0;
    for(size_t i=0;i<bytes/sizeof(uint32_t);++i) {
        uint32_t value=words[i];
        if(value!=pattern) { if(!mismatches) { first=i; first_value=value; } ++mismatches; }
    }
    uint32_t head=words[0],tail=words[bytes/sizeof(uint32_t)-1];
    if(dma_cpu_sync(cpu_fd,PROBE_DMA_BUF_SYNC_READ|PROBE_DMA_BUF_SYNC_END,"END_READ")<0) {
        RESULT("dmabuf-gpu","BLOCKED read-end sync; no PASS emitted"); goto done;
    }
    LOG("DMA GPU readback words=%zu expected=0x%08" PRIx32 " head=0x%08" PRIx32 " tail=0x%08" PRIx32 " mismatches=%zu",bytes/sizeof(uint32_t),pattern,head,tail,mismatches);
    if(mismatches) RESULT("dmabuf-gpu","FAIL firstWord=%zu actual=0x%08" PRIx32 " mismatches=%zu",first,first_value,mismatches);
    else RESULT("dmabuf-gpu","PASS vkCmdFillBuffer + fence + DMA_BUF_SYNC + mmap: all 1024 words equal 0x1234abcd; transfer execution only, not shader/render/performance proof");
done:
    if(fence) vkDestroyFence(dev,fence,NULL);
    if(pool) vkDestroyCommandPool(dev,pool,NULL);
    if(mapping!=MAP_FAILED) munmap(mapping,bytes);
}
static void dma_probe(VkPhysicalDevice pd,VkDevice dev,uint32_t family,const VkPhysicalDeviceMemoryProperties *mp) {
    VkExternalMemoryBufferCreateInfo ext={.sType=VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO,
        .handleTypes=VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT};
    VkBufferCreateInfo bi={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.pNext=&ext,.size=4096,
        .usage=VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,.sharingMode=VK_SHARING_MODE_EXCLUSIVE};
    VkPhysicalDeviceExternalBufferInfo capin={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO,
        .usage=bi.usage,.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT};
    VkExternalBufferProperties cap={.sType=VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES};
    vkGetPhysicalDeviceExternalBufferProperties(pd,&capin,&cap);
    if (!(cap.externalMemoryProperties.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT)) {
        RESULT("dmabuf","UNSUPPORTED buffer not importable; no fd/property/allocation calls"); return;
    }
    PFN_vkGetMemoryFdPropertiesKHR fdprops=(PFN_vkGetMemoryFdPropertiesKHR)vkGetDeviceProcAddr(dev,"vkGetMemoryFdPropertiesKHR");
    if (!fdprops) { RESULT("dmabuf","ERROR enabled extension missing function"); return; }
    VkBuffer b=VK_NULL_HANDLE; VkDeviceMemory mem=VK_NULL_HANDLE;
    int backing=-1,ud=-1,fd=-1,cpu_fd=-1;
    VkResult r=vkCreateBuffer(dev,&bi,NULL,&b);
    if (r!=VK_SUCCESS) { RESULT("dmabuf","create-buffer result=%d",r); return; }
    VkMemoryDedicatedRequirements dedicated={.sType=VK_STRUCTURE_TYPE_MEMORY_DEDICATED_REQUIREMENTS};
    VkMemoryRequirements2 req={.sType=VK_STRUCTURE_TYPE_MEMORY_REQUIREMENTS_2,.pNext=&dedicated};
    VkBufferMemoryRequirementsInfo2 reqin={.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_REQUIREMENTS_INFO_2,.buffer=b};
    vkGetBufferMemoryRequirements2(dev,&reqin,&req);
    LOG("DMA requirements size=%" PRIu64 " alignment=%" PRIu64 " bits=0x%x requiresDedicated=%u prefersDedicated=%u",
        req.memoryRequirements.size,req.memoryRequirements.alignment,req.memoryRequirements.memoryTypeBits,dedicated.requiresDedicatedAllocation,dedicated.prefersDedicatedAllocation);
    long page=sysconf(_SC_PAGESIZE);
    if (page<=0 || req.memoryRequirements.size>UINT64_MAX-(uint64_t)page) { RESULT("dmabuf","ERROR invalid size/page"); goto done; }
    uint64_t size=(req.memoryRequirements.size+(uint64_t)page-1)/(uint64_t)page*(uint64_t)page;
    backing=memfd_create("mali-vulkan-probe",MFD_CLOEXEC|MFD_ALLOW_SEALING);
    if(backing<0) { RESULT("dmabuf","BLOCKED memfd_create errno=%d %s",errno,strerror(errno)); goto done; }
    if(ftruncate(backing,(off_t)size)<0 || fcntl(backing,F_ADD_SEALS,F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_SEAL)<0) {
        RESULT("dmabuf","BLOCKED ftruncate/seals errno=%d %s",errno,strerror(errno)); goto done;
    }
    LOG("DMA backingSize=%" PRIu64 " seals=0x%x",size,fcntl(backing,F_GET_SEALS));
    ud=open("/dev/udmabuf",O_RDWR|O_CLOEXEC);
    if(ud<0) { RESULT("dmabuf","BLOCKED open /dev/udmabuf errno=%d %s (kernel/device/SELinux access)",errno,strerror(errno)); goto done; }
    struct probe_udmabuf_create create={.memfd=(uint32_t)backing,.flags=1,.size=size};
    fd=ioctl(ud,PROBE_UDMABUF_CREATE,&create);
    if(fd<0) { RESULT("dmabuf","BLOCKED UDMABUF_CREATE errno=%d %s",errno,strerror(errno)); goto done; }
    /* Keep an independent fd for mmap/SYNC; successful Vulkan import consumes fd. */
    cpu_fd=fcntl(fd,F_DUPFD_CLOEXEC,0);
    if(cpu_fd<0) { RESULT("dmabuf-gpu","BLOCKED duplicate dma-buf errno=%d %s",errno,strerror(errno)); goto done; }
    VkMemoryFdPropertiesKHR fp={.sType=VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR};
    r=fdprops(dev,VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,fd,&fp);
    LOG("DMA vkGetMemoryFdPropertiesKHR result=%d fdBits=0x%x",r,fp.memoryTypeBits);
    if(r!=VK_SUCCESS) { RESULT("dmabuf","FAIL fd-properties result=%d",r); goto done; }
    uint32_t bits=fp.memoryTypeBits&req.memoryRequirements.memoryTypeBits;
    int index=type_index(bits,mp);
    LOG("DMA intersection=0x%x selectedType=%d",bits,index);
    if(index<0) { RESULT("dmabuf","FAIL no compatible memory type"); goto done; }
    bool need=dedicated.requiresDedicatedAllocation ||
        !!(cap.externalMemoryProperties.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT);
    VkMemoryDedicatedAllocateInfo di={.sType=VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO,.buffer=b};
    VkImportMemoryFdInfoKHR import={.sType=VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,.pNext=need?&di:NULL,
        .handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,.fd=fd};
    VkMemoryAllocateInfo ai={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.pNext=&import,
        .allocationSize=req.memoryRequirements.size,.memoryTypeIndex=(uint32_t)index};
    r=vkAllocateMemory(dev,&ai,NULL,&mem);
    LOG("DMA vkAllocateMemory result=%d dedicated=%u allocationSize=%" PRIu64 " backingSize=%" PRIu64,r,need,req.memoryRequirements.size,size);
    if(r!=VK_SUCCESS) { RESULT("dmabuf","FAIL import allocation result=%d (caller retains fd)",r); goto done; }
    fd=-1; /* Vulkan owns fd ONLY after successful import. */
    r=vkBindBufferMemory(dev,b,mem,0);
    RESULT("dmabuf","%s vkBindBufferMemory result=%d (bind stage only; execution reported separately)",r==VK_SUCCESS?"PASS":"FAIL",r);
    if(r==VK_SUCCESS) dma_gpu_fill(dev,family,b,cpu_fd);
done:
    vkDestroyBuffer(dev,b,NULL);
    if(mem) vkFreeMemory(dev,mem,NULL);
    if(cpu_fd>=0) close(cpu_fd);
    if(fd>=0) close(fd);
    if(ud>=0) close(ud);
    if(backing>=0) close(backing);
}
static void ahb_probe(VkDevice dev,const VkPhysicalDeviceMemoryProperties *mp) {
    PFN_vkGetAndroidHardwareBufferPropertiesANDROID props=(PFN_vkGetAndroidHardwareBufferPropertiesANDROID)
        vkGetDeviceProcAddr(dev,"vkGetAndroidHardwareBufferPropertiesANDROID");
    if(!props) { RESULT("ahb","ERROR enabled extension missing function"); return; }
    AHardwareBuffer_Desc desc={.width=64,.height=64,.layers=1,.format=AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM,
        .usage=AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE};
    AHardwareBuffer *ahb=NULL; VkImage image=VK_NULL_HANDLE; VkDeviceMemory mem=VK_NULL_HANDLE;
    int a=AHardwareBuffer_allocate(&desc,&ahb);
    LOG("AHB allocate result=%d",a);
    if(a || !ahb) { RESULT("ahb","BLOCKED allocation result=%d",a); return; }
    AHardwareBuffer_describe(ahb,&desc);
    LOG("AHB desc=%ux%u layers=%u format=%u stride=%u usage=0x%" PRIx64,desc.width,desc.height,desc.layers,desc.format,desc.stride,desc.usage);
    VkAndroidHardwareBufferFormatPropertiesANDROID fmt={.sType=VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_FORMAT_PROPERTIES_ANDROID};
    VkAndroidHardwareBufferPropertiesANDROID p={.sType=VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_PROPERTIES_ANDROID,.pNext=&fmt};
    VkResult r=props(dev,ahb,&p);
    LOG("AHB properties result=%d size=%" PRIu64 " bits=0x%x vkFormat=%d externalFormat=%" PRIu64 " features=0x%x",r,p.allocationSize,p.memoryTypeBits,fmt.format,fmt.externalFormat,fmt.formatFeatures);
    if(r!=VK_SUCCESS) { RESULT("ahb","FAIL properties result=%d",r); goto done; }
    if(fmt.format!=VK_FORMAT_R8G8B8A8_UNORM || !(fmt.formatFeatures&VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT)) {
        RESULT("ahb","UNSUPPORTED expected RGBA8 sampled image; no invalid import attempted"); goto done;
    }
    VkExternalMemoryImageCreateInfo ei={.sType=VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
        .handleTypes=VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
    VkImageCreateInfo ci={.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,.pNext=&ei,.imageType=VK_IMAGE_TYPE_2D,
        .format=fmt.format,.extent={desc.width,desc.height,1},.mipLevels=1,.arrayLayers=1,.samples=VK_SAMPLE_COUNT_1_BIT,
        .tiling=VK_IMAGE_TILING_OPTIMAL,.usage=VK_IMAGE_USAGE_SAMPLED_BIT,.sharingMode=VK_SHARING_MODE_EXCLUSIVE,
        .initialLayout=VK_IMAGE_LAYOUT_UNDEFINED};
    r=vkCreateImage(dev,&ci,NULL,&image);
    LOG("AHB vkCreateImage result=%d",r);
    if(r!=VK_SUCCESS) { RESULT("ahb","FAIL create-image result=%d",r); goto done; }
    /* AHB image memory requirements must NOT be queried before binding. Use AHB properties. */
    int index=type_index(p.memoryTypeBits,mp);
    if(index<0 || !p.allocationSize) { RESULT("ahb","FAIL invalid allocation properties"); goto done; }
    VkMemoryDedicatedAllocateInfo dedicated={.sType=VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO,.image=image};
    VkImportAndroidHardwareBufferInfoANDROID import={.sType=VK_STRUCTURE_TYPE_IMPORT_ANDROID_HARDWARE_BUFFER_INFO_ANDROID,
        .pNext=&dedicated,.buffer=ahb};
    VkMemoryAllocateInfo ai={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.pNext=&import,.allocationSize=p.allocationSize,.memoryTypeIndex=(uint32_t)index};
    r=vkAllocateMemory(dev,&ai,NULL,&mem);
    LOG("AHB vkAllocateMemory result=%d selectedType=%d",r,index);
    if(r!=VK_SUCCESS) { RESULT("ahb","FAIL import allocation result=%d",r); goto done; }
    r=vkBindImageMemory(dev,image,mem,0);
    RESULT("ahb","%s vkBindImageMemory result=%d (control: allocation/import/bind only)",r==VK_SUCCESS?"PASS":"FAIL",r);
done:
    if(image) vkDestroyImage(dev,image,NULL);
    if(mem) vkFreeMemory(dev,mem,NULL);
    AHardwareBuffer_release(ahb);
}
int main(void) {
    setvbuf(stdout,NULL,_IONBF,0);
    struct utsname un; if(!uname(&un)) LOG("SYSTEM %s %s %s %s",un.sysname,un.release,un.version,un.machine);
    LOG("PROBE mali-dmabuf-v2 build=%s VulkanHeaders=%u AndroidAPI=%d; DMA-BUF GPU fill/fence/mmap verification; AHB bind-only; no shader/render/throughput benchmark",PROBE_REVISION,VK_HEADER_VERSION,__ANDROID_API__);
    uint32_t loader=VK_API_VERSION_1_0;
    PFN_vkEnumerateInstanceVersion version=(PFN_vkEnumerateInstanceVersion)vkGetInstanceProcAddr(NULL,"vkEnumerateInstanceVersion");
    if(version) version(&loader);
    LOG("LOADER api=%u.%u.%u",VK_API_VERSION_MAJOR(loader),VK_API_VERSION_MINOR(loader),VK_API_VERSION_PATCH(loader));
    if(loader<VK_API_VERSION_1_1) { RESULT("instance","UNSUPPORTED Vulkan 1.1 required; no extension functions called"); return 2; }
    VkApplicationInfo app={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.pApplicationName="mali-dmabuf-probe",.apiVersion=VK_API_VERSION_1_1};
    VkInstanceCreateInfo ci={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&app};
    VkInstance instance=VK_NULL_HANDLE; VkResult r=vkCreateInstance(&ci,NULL,&instance);
    if(r) { RESULT("instance","FAIL result=%d",r); return 1; }
    uint32_t n=0; r=vkEnumeratePhysicalDevices(instance,&n,NULL);
    if(r || !n) { RESULT("devices","FAIL result=%d count=%u",r,n); vkDestroyInstance(instance,NULL); return 1; }
    VkPhysicalDevice *devices=calloc(n,sizeof(*devices));
    if(!devices) { vkDestroyInstance(instance,NULL); return 1; }
    r=vkEnumeratePhysicalDevices(instance,&n,devices);
    if(r!=VK_SUCCESS) { free(devices); vkDestroyInstance(instance,NULL); return 1; }
    LOG("DEVICES count=%u",n);
    for(uint32_t d=0;d<n;++d) {
        VkPhysicalDevice pd=devices[d]; VkPhysicalDeviceProperties p; vkGetPhysicalDeviceProperties(pd,&p);
        LOG("DEVICE index=%u name=%s vendor=0x%x device=0x%x driver=0x%x api=%u.%u.%u",d,p.deviceName,p.vendorID,p.deviceID,p.driverVersion,VK_API_VERSION_MAJOR(p.apiVersion),VK_API_VERSION_MINOR(p.apiVersion),VK_API_VERSION_PATCH(p.apiVersion));
        uint32_t count=0; r=vkEnumerateDeviceExtensionProperties(pd,NULL,&count,NULL);
        if(r!=VK_SUCCESS) { RESULT("extensions","FAIL enumeration result=%d",r); continue; }
        VkExtensionProperties *extensions=calloc(count?count:1,sizeof(*extensions));
        if(!extensions) continue;
        r=vkEnumerateDeviceExtensionProperties(pd,NULL,&count,extensions);
        if(r!=VK_SUCCESS) { RESULT("extensions","FAIL enumeration result=%d",r); free(extensions); continue; }
        for(uint32_t i=0;i<count;++i) LOG("EXT device=%u %s spec=%u",d,extensions[i].extensionName,extensions[i].specVersion);
        const char *names[]={VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME,VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME,
            VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME,VK_ANDROID_EXTERNAL_MEMORY_ANDROID_HARDWARE_BUFFER_EXTENSION_NAME,
            VK_KHR_EXTERNAL_MEMORY_EXTENSION_NAME,VK_KHR_DEDICATED_ALLOCATION_EXTENSION_NAME,
            VK_KHR_GET_MEMORY_REQUIREMENTS_2_EXTENSION_NAME,VK_KHR_BIND_MEMORY_2_EXTENSION_NAME,
            VK_KHR_SAMPLER_YCBCR_CONVERSION_EXTENSION_NAME,VK_EXT_QUEUE_FAMILY_FOREIGN_EXTENSION_NAME,
            VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME};
        for(unsigned i=0;i<sizeof(names)/sizeof(names[0]);++i) LOG("SUPPORT %s=%s",names[i],has(extensions,count,names[i])?"YES":"NO");
        bool fd=has(extensions,count,names[0]),dma=has(extensions,count,names[1]);
        bool ahb=has(extensions,count,names[3]) && has(extensions,count,VK_EXT_QUEUE_FAMILY_FOREIGN_EXTENSION_NAME);
        if(p.apiVersion<VK_API_VERSION_1_1) { RESULT("device","UNSUPPORTED Vulkan 1.1 required; skipped"); free(extensions); continue; }
        VkPhysicalDeviceMemoryProperties mp; vkGetPhysicalDeviceMemoryProperties(pd,&mp);
        for(uint32_t i=0;i<mp.memoryTypeCount;++i) LOG("MEMORY type=%u heap=%u flags=0x%x",i,mp.memoryTypes[i].heapIndex,mp.memoryTypes[i].propertyFlags);
        VkExternalMemoryHandleTypeFlagBits handles[6]; unsigned nh=0;
        if(fd) handles[nh++]=VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        if(fd && dma) handles[nh++]=VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        if(has(extensions,count,names[2])) {
            handles[nh++]=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
            handles[nh++]=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_MAPPED_FOREIGN_MEMORY_BIT_EXT;
            VkPhysicalDeviceExternalMemoryHostPropertiesEXT hp={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT};
            VkPhysicalDeviceProperties2 pp={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,.pNext=&hp};
            vkGetPhysicalDeviceProperties2(pd,&pp); LOG("HOST minImportedHostPointerAlignment=%" PRIu64,hp.minImportedHostPointerAlignment);
        }
        if(ahb) handles[nh++]=VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID;
        for(unsigned h=0;h<nh;++h) { buffer_caps(pd,handles[h]); image_caps(pd,handles[h]); }
        if(!fd) RESULT("dmabuf","UNSUPPORTED VK_KHR_external_memory_fd absent; no fd functions called");
        else if(!dma) RESULT("dmabuf","UNSUPPORTED VK_EXT_external_memory_dma_buf absent; no DMA_BUF calls");
        uint32_t nq=0; vkGetPhysicalDeviceQueueFamilyProperties(pd,&nq,NULL);
        VkQueueFamilyProperties *q=calloc(nq?nq:1,sizeof(*q));
        if(!q) { free(extensions); continue; }
        vkGetPhysicalDeviceQueueFamilyProperties(pd,&nq,q);
        uint32_t family=0; while(family<nq && (!q[family].queueCount || !(q[family].queueFlags&(VK_QUEUE_GRAPHICS_BIT|VK_QUEUE_COMPUTE_BIT|VK_QUEUE_TRANSFER_BIT)))) ++family;
        free(q);
        if(family==nq) { RESULT("device","FAIL no queue family"); free(extensions); continue; }
        const char *enabled[3]; uint32_t ne=0;
        if(fd && dma) { enabled[ne++]=names[0]; enabled[ne++]=names[1]; }
        /* Four extensions can be needed when both paths are available. */
        const char *all[4]; for(uint32_t i=0;i<ne;++i) all[i]=enabled[i];
        if(ahb) { all[ne++]=names[3]; all[ne++]=VK_EXT_QUEUE_FAMILY_FOREIGN_EXTENSION_NAME; }
        float priority=1; VkDeviceQueueCreateInfo qi={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=family,.queueCount=1,.pQueuePriorities=&priority};
        VkDeviceCreateInfo dc={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&qi,
            .enabledExtensionCount=ne,.ppEnabledExtensionNames=all};
        VkDevice dev=VK_NULL_HANDLE; r=vkCreateDevice(pd,&dc,NULL,&dev);
        LOG("DEVICE create result=%d enabledExtensions=%u queueFamily=%u",r,ne,family);
        if(r==VK_SUCCESS) {
            if(fd && dma) dma_probe(pd,dev,family,&mp);
            if(ahb) ahb_probe(dev,&mp); else RESULT("ahb","UNSUPPORTED AHB/queue_family_foreign extension absent");
            vkDestroyDevice(dev,NULL);
        } else RESULT("device","FAIL create result=%d",r);
        free(extensions);
    }
    free(devices); vkDestroyInstance(instance,NULL);
    LOG("PROBE_DONE (exit 0 means diagnostic completed, not that all import paths passed)");
    return 0;
}
