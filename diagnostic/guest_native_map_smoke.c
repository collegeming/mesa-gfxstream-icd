#define _POSIX_C_SOURCE 200809L
#include <vulkan/vulkan.h>
#include <inttypes.h>
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

/* This is an accidental-run interlock, NOT GPU user consent. Driver internals
 * may perform activity during initialization/allocation/mapping. No application
 * workload API or mapped payload access is permitted here. */
int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "--allow-map-inspection") != 0) {
        LOG("RESULT stage=cli FAIL exit=2 usage: guest_native_map_smoke --allow-map-inspection; flag is NOT GPU user consent; no Vulkan initialized");
        return 2;
    }
    LOG("PROBE guest-native-map-smoke-v1 commit=%s VulkanHeaders=%u", PROBE_REVISION, VK_HEADER_VERSION);
    LOG("BOUNDARY flag is NOT GPU user consent; parent source safety review required before any device action");
    LOG("BOUNDARY no application GPU workload; Vulkan initialization/allocation/mapping may cause driver-internal GPU activity");
    LOG("BOUNDARY no mapped payload reads/writes; no resources/bindings/commands/shaders/queue retrieval/submission; no manual mapping or KVM");
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
        .pNext = NULL, .pApplicationName = "guest-native-map-smoke", .apiVersion = VK_API_VERSION_1_1};
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
    status = 0;
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
    LOG("RESULT stage=done %s exit=%d; success means allocation/map lifetime calls returned only; no payload, coherence, backing or renderer achievement", status == 0 ? "PASS" : "FAIL", status);
    return status;
}
