#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>

#define EXPECTED_DEVICE "Mali-G2-Ultra-NX MC16"
#define MEMORY_TYPE_INDEX 8u
#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)

/* Only scalar metadata is extracted from proc text, never mapped payload. */
struct vma {
    uintptr_t start, end;
    uint64_t offset;
    char permissions[5];
};

static bool parse_vma(const char *line, struct vma *vma) {
    return sscanf(line, "%" SCNxPTR "-%" SCNxPTR " %4s %" SCNx64,
                  &vma->start, &vma->end, vma->permissions, &vma->offset) == 4 &&
           vma->start < vma->end;
}

static bool contains(const struct vma *vma, uintptr_t pointer) {
    return vma->start <= pointer && pointer < vma->end;
}

static void print_vma(const char *label, const struct vma *vma, uintptr_t pointer,
                      uint64_t requested_size) {
    LOG("%s start=0x%" PRIxPTR " end=0x%" PRIxPTR " permissions=%s offset=0x%" PRIx64
        " pointerOffsetInVMA=%" PRIuPTR " requestedRangeWithinThisVMA=%u",
        label, vma->start, vma->end, vma->permissions, vma->offset,
        pointer - vma->start, requested_size <= vma->end - pointer);
}

static bool inspect_vma(uintptr_t pointer, uint64_t requested_size) {
    char line[4096];
    struct vma maps = {0}, smaps = {0}, current;
    bool maps_found = false, smaps_found = false;
    bool vmflags = false, kernel_page = false, mmu_page = false;
    FILE *file = fopen("/proc/self/maps", "re");
    if (!file) {
        LOG("RESULT FAIL maps open errno=%d", errno);
        return false;
    }
    while (fgets(line, sizeof(line), file)) {
        if (parse_vma(line, &current) && contains(&current, pointer)) {
            maps = current;
            maps_found = true;
            print_vma("MAPS_VMA", &maps, pointer, requested_size);
            break;
        }
    }
    bool maps_error = ferror(file) != 0;
    if (fclose(file) != 0) maps_error = true;
    if (!maps_found || maps_error) {
        LOG("RESULT FAIL maps matchingVMA=%u readError=%u", maps_found, maps_error);
        return false;
    }
    file = fopen("/proc/self/smaps", "re");
    if (!file) {
        LOG("RESULT FAIL smaps open errno=%d", errno);
        return false;
    }
    bool matching = false;
    while (fgets(line, sizeof(line), file)) {
        if (parse_vma(line, &current)) {
            if (matching) break;
            matching = contains(&current, pointer);
            if (matching) {
                smaps = current;
                smaps_found = true;
                print_vma("SMAPS_VMA", &smaps, pointer, requested_size);
            }
        } else if (matching) {
            if (!strncmp(line, "VmFlags:", 8)) {
                LOG("SMAPS %s", line);
                vmflags = true;
            } else if (!strncmp(line, "KernelPageSize:", 15)) {
                LOG("SMAPS %s", line);
                kernel_page = true;
            } else if (!strncmp(line, "MMUPageSize:", 12)) {
                LOG("SMAPS %s", line);
                mmu_page = true;
            }
        }
    }
    bool smaps_error = ferror(file) != 0;
    if (fclose(file) != 0) smaps_error = true;
    bool same = smaps_found && maps.start == smaps.start && maps.end == smaps.end &&
        maps.offset == smaps.offset && !strcmp(maps.permissions, smaps.permissions);
    LOG("VMA_METADATA matchingMapsSmaps=%u VmFlags=%u KernelPageSize=%u MMUPageSize=%u readError=%u",
        same, vmflags, kernel_page, mmu_page, smaps_error);
    LOG("LIMIT VMA boundaries are process metadata, NOT Vulkan allocation extent; proc snapshots are not atomic; VmFlags/page sizes do not prove backing identity, physical contiguity or coherence");
    return same && vmflags && kernel_page && mmu_page && !smaps_error;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    LOG("PROBE mali-native-map-query-v1 commit=%s VulkanHeaders=%u AndroidAPI=%d",
        PROBE_REVISION, VK_HEADER_VERSION, __ANDROID_API__);
    LOG("SAFETY inspect-only native ordinary type8 allocation, one CPU map/unmap; no mapped payload reads/writes; no external import/export, resources, bindings, commands, queue retrieval/submission, guest mapping or KVM operations");
    LOG("BOUNDARY no application GPU workload/submission; NOT a guarantee of zero driver-internal GPU activity: Vulkan initialization/allocation/mapping may invoke driver internals");
    LOG("LIMIT no coherence execution test, cache test, guest aliasing test, backing identity proof or allocation-extent inference");
    struct utsname system_info;
    if (!uname(&system_info)) LOG("SYSTEM %s %s %s", system_info.sysname, system_info.release, system_info.machine);
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0 || page > 65536 || (page & (page - 1))) {
        LOG("RESULT FAIL invalid page size=%ld maximum=65536", page);
        return 1;
    }
    uint64_t size = page > 4096 ? (uint64_t)page : 4096;
    LOG("REQUEST allocationSize=%" PRIu64 " systemPageSize=%ld maximum=65536 memoryTypeIndex=%u allocationPNext=NULL mapCount=1 mapOffset=0 mapSize=requestedAllocationSize", size, page, MEMORY_TYPE_INDEX);
    VkApplicationInfo application = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "mali-native-map-query", .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo instance_info = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pNext = NULL, .pApplicationInfo = &application,
        .enabledLayerCount = 0, .enabledExtensionCount = 0};
    VkInstance instance = VK_NULL_HANDLE;
    VkResult result = vkCreateInstance(&instance_info, NULL, &instance);
    LOG("CREATE_INSTANCE result=%d", result);
    if (result != VK_SUCCESS) return 1;
    int status = 1;
    VkDevice device = VK_NULL_HANDLE;
    VkDeviceMemory allocation = VK_NULL_HANDLE;
    bool mapped = false;
    void *mapped_pointer = NULL;
    uint32_t count = 0;
    result = vkEnumeratePhysicalDevices(instance, &count, NULL);
    if (result != VK_SUCCESS || !count || count > 16) {
        LOG("RESULT FAIL physicalDeviceCount=%u result=%d", count, result);
        goto done;
    }
    VkPhysicalDevice devices[16];
    result = vkEnumeratePhysicalDevices(instance, &count, devices);
    if (result != VK_SUCCESS) { LOG("RESULT FAIL enumerate result=%d", result); goto done; }
    VkPhysicalDevice selected = VK_NULL_HANDLE;
    uint32_t matches = 0;
    VkPhysicalDeviceProperties selected_properties = {0};
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties properties;
        vkGetPhysicalDeviceProperties(devices[i], &properties);
        LOG("DEVICE name=%s vendor=0x%x device=0x%x driver=0x%x api=%u.%u.%u", properties.deviceName,
            properties.vendorID, properties.deviceID, properties.driverVersion,
            VK_API_VERSION_MAJOR(properties.apiVersion), VK_API_VERSION_MINOR(properties.apiVersion),
            VK_API_VERSION_PATCH(properties.apiVersion));
        if (!strcmp(properties.deviceName, EXPECTED_DEVICE) && properties.vendorID == 0x13b5 &&
            properties.apiVersion >= VK_API_VERSION_1_1) {
            selected = devices[i]; selected_properties = properties; ++matches;
        }
    }
    if (matches != 1) { LOG("RESULT FAIL exact expected Mali matches=%u; no fallback", matches); goto done; }
    VkPhysicalDeviceMemoryProperties memory;
    vkGetPhysicalDeviceMemoryProperties(selected, &memory);
    if (memory.memoryTypeCount <= MEMORY_TYPE_INDEX) { LOG("RESULT FAIL type8 absent"); goto done; }
    VkMemoryType type = memory.memoryTypes[MEMORY_TYPE_INDEX];
    VkMemoryPropertyFlags required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    LOG("TYPE index=%u flags=0x%x heap=%u HOST_VISIBLE=%u HOST_COHERENT=%u", MEMORY_TYPE_INDEX,
        type.propertyFlags, type.heapIndex, !!(type.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT),
        !!(type.propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT));
    if ((type.propertyFlags & required) != required ||
        (type.propertyFlags & VK_MEMORY_PROPERTY_PROTECTED_BIT) ||
        type.heapIndex >= memory.memoryHeapCount || memory.memoryHeaps[type.heapIndex].size < size) {
        LOG("RESULT FAIL type8 flags/heap rejected; no fallback"); goto done;
    }
    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, NULL);
    if (!queue_count || queue_count > 256) { LOG("RESULT FAIL queue family count=%u", queue_count); goto done; }
    VkQueueFamilyProperties families[256];
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, families);
    uint32_t family = 0;
    while (family < queue_count && !families[family].queueCount) ++family;
    if (family == queue_count) { LOG("RESULT FAIL no queue family"); goto done; }
    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .pNext = NULL, .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    VkDeviceCreateInfo device_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .pNext = NULL, .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue_info,
        .enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL};
    LOG("CREATE_DEVICE family=%u queueCount=1 initialization only; never retrieve or use queue; extensions=0 features=NULL pNext=NULL", family);
    result = vkCreateDevice(selected, &device_info, NULL, &device);
    LOG("CREATE_DEVICE result=%d", result);
    if (result != VK_SUCCESS) goto done;
    VkMemoryAllocateInfo allocation_info = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX};
    result = vkAllocateMemory(device, &allocation_info, NULL, &allocation);
    LOG("ALLOCATE result=%d requestedAllocationSize=%" PRIu64 " type=8 pNext=NULL", result, size);
    if (result != VK_SUCCESS) goto done;
    result = vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer);
    LOG("MAP result=%d", result);
    if (result != VK_SUCCESS) goto done;
    mapped = true;
    uintptr_t address = (uintptr_t)mapped_pointer;
    if (!address) { LOG("RESULT FAIL map returned NULL"); goto done; }
    LOG("MAP_POINTER address=0x%" PRIxPTR " systemPageSize=%ld pageRemainder=%" PRIuPTR
        " pageAligned=%u minMemoryMapAlignment=%zu requestedAllocationSize=%" PRIu64,
        address, page, address % (uintptr_t)page, address % (uintptr_t)page == 0,
        selected_properties.limits.minMemoryMapAlignment, size);
    if (size > UINTPTR_MAX - address) { LOG("RESULT FAIL requested range overflow"); goto done; }
    status = inspect_vma(address, size) ? 0 : 2;
done:
    if (mapped) { vkUnmapMemory(device, allocation); LOG("CLEANUP unmapped"); }
    if (allocation != VK_NULL_HANDLE) { vkFreeMemory(device, allocation, NULL); LOG("CLEANUP freed"); }
    if (device != VK_NULL_HANDLE) { vkDestroyDevice(device, NULL); LOG("CLEANUP device destroyed"); }
    vkDestroyInstance(instance, NULL);
    LOG("PROBE_DONE status=%d; status0 means metadata inspected only, not coherence/backing/guest compatibility success", status);
    return status;
}
