#define _POSIX_C_SOURCE 200809L
#ifndef MAP_IDENTITY_CPU_ONLY
#include <vulkan/vulkan.h>
#endif
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <time.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef PROBE_REVISION
#define PROBE_REVISION "unversioned"
#endif
#define MEMORY_TYPE_INDEX 8u
#define REQUESTED_SIZE 4096u
#define MAX_DEVICES 16u
#define MAX_FAMILIES 256u
#define EXPECTED_ICD "/opt/droidvm-mesa-reference/icd.json"
#define EXPECTED_LIBRARY_PREFIX "/opt/droidvm-mesa-reference/usr/local/lib/aarch64-linux-gnu"
#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)

/* Pure helpers exercised on ordinary CPU memory only in Actions. */
struct page_identity { unsigned present, swapped; uint64_t pfn; };
static struct page_identity decode_pagemap(uint64_t raw) {
    return (struct page_identity){(unsigned)(raw >> 63), (unsigned)((raw >> 62) & 1u), raw & ((UINT64_C(1) << 55) - 1u)};
}
static int pagemap_offset(uintptr_t va, uint64_t page, off_t *offset) {
    if (!page || (page & (page - 1u)) || va / page > (uint64_t)INT64_MAX / 8u) return 1;
    *offset = (off_t)((va / page) * 8u); return 0;
}
static int fault_and_mark(void *base, size_t extent) {
    uintptr_t start = (uintptr_t)base;
    if (!base || extent != REQUESTED_SIZE || start % _Alignof(uint32_t) ||
        start > UINTPTR_MAX - (REQUESTED_SIZE - 1u) ||
        4092u > extent || sizeof(uint32_t) > extent - 4092u) return 20;
    volatile uint32_t *first = (volatile uint32_t *)base;
    volatile uint32_t *last = (volatile uint32_t *)((unsigned char *)base + 4092u);
    LOG("BEGIN stage=first-fault-read offset=0 width=4; root loss/kernel hang possible");
    uint32_t initial = *first;
    LOG("RESULT stage=first-fault-read RETURNED value=0x%08" PRIx32 " uninitialized; no expected value", initial);
    LOG("BEGIN stage=marker-write offset=0 width=4 marker=0x13579bdf");
    *first = UINT32_C(0x13579bdf);
    LOG("BEGIN stage=marker-write offset=4092 width=4 marker=0x2468ace0");
    *last = UINT32_C(0x2468ace0);
    return 0;
}

#ifndef MAP_IDENTITY_CPU_ONLY
/* Open only own maps and own pagemap, read-only. Never fdinfo, devmem or host hooks. */
static int own_mapping_metadata(uintptr_t address, uint64_t page) {
    int status = 0;
    FILE *maps = fopen("/proc/self/maps", "r");
    if (!maps) { LOG("RESULT stage=maps FAIL errno=%d", errno); status = 23; }
    else {
        char line[4096]; unsigned found = 0;
        while (fgets(line, sizeof(line), maps)) {
            uintptr_t low, high;
            if (sscanf(line, "%" SCNxPTR "-%" SCNxPTR, &low, &high) == 2 &&
                low <= address && address < high) {
                /* A line can include /dev/dri/renderD128 (or the actual DRM node).
                 * Print the matched entry only, never the rest of this process. */
                LOG("PROC_MAP_MATCH %s", line); ++found;
                if (address + 4092u >= high) status = 23;
            }
        }
        if (ferror(maps) || found != 1) status = 23;
        if (fclose(maps)) status = 23;
        LOG("RESULT stage=maps %s matches=%u; VMA bounds NOT allocation/blob extent", status ? "FAIL" : "RECORDED", found);
    }
    int fd = open("/proc/self/pagemap", O_RDONLY | O_CLOEXEC);
    if (fd < 0) { LOG("RESULT stage=pagemap-open FAIL errno=%d; metadata unavailable, not a pass", errno); return 24; }
    const uintptr_t addresses[2] = {address, address + 4092u};
    for (unsigned i = 0; i < 2; ++i) {
        off_t offset; uint64_t raw = 0;
        if (pagemap_offset(addresses[i], page, &offset)) { status = 24; LOG("RESULT stage=pagemap-offset FAIL"); continue; }
        ssize_t bytes = pread(fd, &raw, sizeof(raw), offset);
        if (bytes != (ssize_t)sizeof(raw)) {
            LOG("RESULT stage=pagemap-read FAIL va=0x%" PRIxPTR " bytes=%zd errno=%d; no retry", addresses[i], bytes, bytes < 0 ? errno : 0);
            status = 24; continue;
        }
        struct page_identity p = decode_pagemap(raw);
        LOG("PAGEMAP va=0x%" PRIxPTR " pageIndex=%" PRIu64 " entryOffset=%" PRIu64 " raw=0x%016" PRIx64 " present=%u swapped=%u low55=0x%" PRIx64,
            addresses[i], (uint64_t)addresses[i] / page, (uint64_t)offset, raw, p.present, p.swapped, p.pfn);
        if (!p.present || p.swapped || !p.pfn || p.pfn > UINT64_MAX / page) {
            LOG("RESULT stage=guest-identity FAIL guestPFN/IPA unavailable; zero may mean PFN redaction or special mapping, NEVER hostPFN or physical zero proof");
            status = 24; continue;
        }
        LOG("GUEST_IDENTITY guestPFN=0x%" PRIx64 " guestIPA_page=0x%" PRIx64 " guestIPA_byte=0x%" PRIx64 " NOT hostPFN; host BAR comparison requires independent parent evidence",
            p.pfn, p.pfn * page, p.pfn * page + addresses[i] % page);
    }
    if (close(fd)) status = 24;
    return status;
}
static int hold_mapping(int status) {
    struct timespec start, end;
    if (clock_gettime(CLOCK_MONOTONIC, &start)) { LOG("RESULT stage=hold-clock FAIL errno=%d", errno); return 25; }
    LOG("HOLD_READY PID=%ld start=%jd.%09ld seconds=45 metadataStatus=%s exitStatus=%d; mapping/allocation/owner remain live for parent inspection",
        (long)getpid(), (intmax_t)start.tv_sec, start.tv_nsec, status ? "FAIL" : "RECORDED", status);
    errno = 0;
    unsigned remaining = sleep(45); /* Exactly once; EINTR is failure, no restart loop. */
    int sleep_error = errno;
    if (clock_gettime(CLOCK_MONOTONIC, &end)) { LOG("RESULT stage=hold-clock-end FAIL errno=%d", errno); return 25; }
    LOG("HOLD_END PID=%ld end=%jd.%09ld remaining=%u errno=%d", (long)getpid(), (intmax_t)end.tv_sec, end.tv_nsec, remaining, sleep_error);
    if (remaining || sleep_error == EINTR || end.tv_sec - start.tv_sec < 45) return 25;
    return status;
}
#endif

#ifndef MAP_IDENTITY_CPU_ONLY
/* Mapping identity interlock only, NOT GPU consent. No automatic retry. */
int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "--allow-mapping-identity") != 0) {
        LOG("RESULT stage=cli FAIL exit=2 usage: guest_native_map_identity --allow-mapping-identity; flag is NOT GPU user consent; no Vulkan initialized");
        LOG("CAUTION first payload access may hang kernel/reboot host and lose root; timeout cannot guarantee recovery; no automatic retry. Parent source review and explicit CPU authorization required.");
        return 2;
    }
    LOG("PROBE guest-native-map-identity-v1 commit=%s VulkanHeaders=%u", PROBE_REVISION, VK_HEADER_VERSION);
    LOG("BOUNDARY flag is NOT GPU user consent; parent source safety review required before any device action");
    LOG("BOUNDARY no application GPU workload; Vulkan initialization/allocation/mapping may cause driver-internal GPU activity");
    LOG("BOUNDARY one volatile uint32 read then two marker writes only; own maps/pagemap metadata; no resources/bindings/commands/shaders/queue retrieval/submission; no manual mapping or KVM");
    LOG("LIMIT requested size only: driver internal real blob may be larger; no actual blob extent, coherence, backing identity or renderer achievement claimed");
    const char *icd = getenv("VK_DRIVER_FILES");
    const char *libraries = getenv("LD_LIBRARY_PATH");
    const char *legacy_icd = getenv("VK_ICD_FILENAMES");
    const char *layers = getenv("VK_INSTANCE_LAYERS");
    const char *preload = getenv("LD_PRELOAD");
    size_t prefix_size = sizeof(EXPECTED_LIBRARY_PREFIX) - 1;
    if (!icd || strcmp(icd, EXPECTED_ICD) != 0 || !libraries ||
        strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0 ||
        (libraries[prefix_size] != '\0' && libraries[prefix_size] != ':') ||
        (legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])) {
        LOG("RESULT stage=environment FAIL exit=3 require VK_DRIVER_FILES=%s and LD_LIBRARY_PATH first entry=%s; reject legacy ICD, explicit layers and preload; no Vulkan initialized", EXPECTED_ICD, EXPECTED_LIBRARY_PREFIX);
        return 3;
    }
    LOG("RESULT stage=environment PASS VK_DRIVER_FILES=%s LD_LIBRARY_PATH=%s; selectors are not a proof of loaded driver identity", icd, libraries);
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0 || page > 65536 || (page & (page - 1)) || page != 4096) {
        LOG("RESULT stage=page FAIL exit=4 systemPageSize=%ld require=4096 maximum=65536 no request enlargement", page);
        return 4;
    }
    const VkDeviceSize size = REQUESTED_SIZE;
    LOG("RESULT stage=page PASS systemPageSize=%ld requestedAllocationSize=%" PRIu64 " memoryTypeIndex=8 pNext=NULL mapOffset=0 mapSize=requestedSize attempts=1", page, (uint64_t)size);
    int status = 10;
    VkInstance instance = VK_NULL_HANDLE;
    VkDevice device = VK_NULL_HANDLE;
    VkDeviceMemory allocation = VK_NULL_HANDLE;
    bool mapped = false;
    void *mapped_pointer = NULL;
    VkApplicationInfo application = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pNext = NULL, .pApplicationName = "guest-native-map-identity", .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo instance_info = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pNext = NULL, .pApplicationInfo = &application,
        .enabledLayerCount = 0, .enabledExtensionCount = 0};
    VkResult result = vkCreateInstance(&instance_info, NULL, &instance);
    LOG("RESULT stage=instance %s VkResult=%d", result == VK_SUCCESS ? "PASS" : "FAIL", result);
    if (result != VK_SUCCESS) goto done;
    status = 11;
    uint32_t count = 0;
    result = vkEnumeratePhysicalDevices(instance, &count, NULL);
    LOG("RESULT stage=enumerate-count %s VkResult=%d count=%u", result == VK_SUCCESS && count > 0 && count <= MAX_DEVICES ? "PASS" : "FAIL", result, count);
    if (result != VK_SUCCESS || !count || count > MAX_DEVICES) goto done;
    status = 12;
    VkPhysicalDevice devices[MAX_DEVICES];
    result = vkEnumeratePhysicalDevices(instance, &count, devices);
    LOG("RESULT stage=enumerate-list %s VkResult=%d count=%u; no retry", result == VK_SUCCESS && count > 0 && count <= MAX_DEVICES ? "PASS" : "FAIL", result, count);
    if (result != VK_SUCCESS || !count || count > MAX_DEVICES) goto done;
    status = 13;
    VkPhysicalDevice selected = VK_NULL_HANDLE;
    VkPhysicalDeviceProperties selected_properties = {0};
    uint32_t matches = 0;
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties properties;
        vkGetPhysicalDeviceProperties(devices[i], &properties);
        LOG("DEVICE index=%u name=%s vendorID=0x%x deviceID=0x%x driverVersion=0x%x api=%u.%u.%u", i, properties.deviceName,
            properties.vendorID, properties.deviceID, properties.driverVersion,
            VK_API_VERSION_MAJOR(properties.apiVersion), VK_API_VERSION_MINOR(properties.apiVersion), VK_API_VERSION_PATCH(properties.apiVersion));
        if ((strstr(properties.deviceName, "Virtio-GPU") || strstr(properties.deviceName, "GFXStream")) &&
            strstr(properties.deviceName, "Mali") && properties.apiVersion >= VK_API_VERSION_1_1) {
            selected = devices[i]; selected_properties = properties; ++matches;
        }
    }
    LOG("RESULT stage=identity %s matchingDevices=%u; require (Virtio-GPU OR GFXStream) AND Mali, Vulkan>=1.1; no fallback", matches == 1 ? "PASS" : "FAIL", matches);
    if (matches != 1) goto done;
    status = 14;
    VkPhysicalDeviceMemoryProperties memory;
    vkGetPhysicalDeviceMemoryProperties(selected, &memory);
    LOG("GUEST_MEMORY memoryTypeCount=%u memoryHeapCount=%u; actual guest-reported flags, not host inference", memory.memoryTypeCount, memory.memoryHeapCount);
    if (memory.memoryTypeCount <= MEMORY_TYPE_INDEX || memory.memoryTypeCount > VK_MAX_MEMORY_TYPES ||
        memory.memoryHeapCount > VK_MAX_MEMORY_HEAPS) {
        LOG("RESULT stage=type8 FAIL invalid memory counts or absent type8"); goto done;
    }
    VkMemoryType type = memory.memoryTypes[MEMORY_TYPE_INDEX];
    VkMemoryPropertyFlags required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    LOG("TYPE index=8 flags=0x%x heap=%u HOST_VISIBLE=%u HOST_COHERENT=%u", type.propertyFlags, type.heapIndex,
        !!(type.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT), !!(type.propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT));
    if ((type.propertyFlags & required) != required ||
        (type.propertyFlags & VK_MEMORY_PROPERTY_PROTECTED_BIT) || type.heapIndex >= memory.memoryHeapCount ||
        memory.memoryHeaps[type.heapIndex].size < size) {
        LOG("RESULT stage=type8 FAIL required guest flags/heap rejected; no substitute memory type"); goto done;
    }
    LOG("RESULT stage=type8 PASS heapSize=%" PRIu64, (uint64_t)memory.memoryHeaps[type.heapIndex].size);
    status = 15;
    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, NULL);
    if (!queue_count || queue_count > MAX_FAMILIES) {
        LOG("RESULT stage=queue-family FAIL count=%u", queue_count); goto done;
    }
    VkQueueFamilyProperties families[MAX_FAMILIES];
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, families);
    uint32_t family = 0;
    while (family < queue_count && !families[family].queueCount) ++family;
    if (family == queue_count) { LOG("RESULT stage=queue-family FAIL no usable family"); goto done; }
    LOG("RESULT stage=queue-family PASS family=%u; initialization only; no queue retrieval", family);
    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .pNext = NULL, .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    VkDeviceCreateInfo device_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .pNext = NULL, .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue_info,
        .enabledExtensionCount = 0, .enabledLayerCount = 0, .pEnabledFeatures = NULL};
    status = 16;
    result = vkCreateDevice(selected, &device_info, NULL, &device);
    LOG("RESULT stage=device %s VkResult=%d extensions=0 features=NULL pNext=NULL", result == VK_SUCCESS ? "PASS" : "FAIL", result);
    if (result != VK_SUCCESS) goto done;
    VkMemoryAllocateInfo allocation_info = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = NULL, .allocationSize = size, .memoryTypeIndex = MEMORY_TYPE_INDEX};
    status = 17;
    LOG("BEGIN stage=allocate requestedAllocationSize=%" PRIu64 " type=8 pNext=NULL; internal blob size unknown", (uint64_t)size);
    result = vkAllocateMemory(device, &allocation_info, NULL, &allocation);
    LOG("RESULT stage=allocate %s VkResult=%d requestedAllocationSize=%" PRIu64, result == VK_SUCCESS ? "PASS" : "FAIL", result, (uint64_t)size);
    if (result != VK_SUCCESS) goto done;
    status = 18;
    LOG("BEGIN stage=map offset=0 requestedMapSize=%" PRIu64, (uint64_t)size);
    result = vkMapMemory(device, allocation, 0, size, 0, &mapped_pointer);
    LOG("RESULT stage=map %s VkResult=%d", result == VK_SUCCESS ? "PASS" : "FAIL", result);
    if (result != VK_SUCCESS) goto done;
    mapped = true;
    status = 19;
    uintptr_t address = (uintptr_t)mapped_pointer;
    size_t alignment = selected_properties.limits.minMemoryMapAlignment;
    if (!address || !alignment) { LOG("RESULT stage=address FAIL null address or zero minMemoryMapAlignment"); goto done; }
    LOG("MAP_ADDRESS address=0x%" PRIxPTR " systemPageSize=%ld pageRemainder=%" PRIuPTR " pageAligned=%u minMemoryMapAlignment=%zu alignmentRemainder=%" PRIuPTR " requestedMapSize=%" PRIu64,
        address, page, address % (uintptr_t)page, address % (uintptr_t)page == 0,
        alignment, address % alignment, (uint64_t)size);
    if (address % alignment != 0) { LOG("RESULT stage=address FAIL Vulkan map alignment"); goto done; }
    LOG("RESULT stage=address PASS pointer metadata only; page alignment is reported, not assumed");
    LOG("MAP_IDENTITY PID=%ld mappedVA=0x%" PRIxPTR " requestedAllocationSize=4096 requestedMapSize=4096; no inferred VMA extent", (long)getpid(), address);
    if (address % (uintptr_t)page) { LOG("RESULT stage=address FAIL require page alignment so base and base+4092 share one page"); goto done; }
    status = fault_and_mark(mapped_pointer, (size_t)size);
    if (status) goto done;
    status = own_mapping_metadata(address, (uint64_t)page);
    LOG("RESULT stage=metadata %s exit=%d; no self-coherence or host backing identity claim", status ? "FAIL" : "RECORDED", status);
    status = hold_mapping(status);
done:
    if (mapped) {
        LOG("BEGIN stage=unmap"); vkUnmapMemory(device, allocation);
        LOG("RESULT stage=unmap RETURNED void API, no VkResult");
    }
    if (allocation != VK_NULL_HANDLE) {
        LOG("BEGIN stage=free"); vkFreeMemory(device, allocation, NULL);
        LOG("RESULT stage=free RETURNED void API, no VkResult");
    }
    if (device != VK_NULL_HANDLE) {
        LOG("BEGIN stage=destroy-device"); vkDestroyDevice(device, NULL);
        LOG("RESULT stage=destroy-device RETURNED void API, no VkResult");
    }
    if (instance != VK_NULL_HANDLE) {
        LOG("BEGIN stage=destroy-instance"); vkDestroyInstance(instance, NULL);
        LOG("RESULT stage=destroy-instance RETURNED void API, no VkResult");
    }
    LOG("RESULT stage=done %s exit=%d; metadata diagnostic only, NOT coherence/backing equivalence proof; no host hook, flush or GPU workload", status == 0 ? "PASS" : "FAIL", status);
    return status;
}
#endif
