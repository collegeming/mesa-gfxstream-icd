#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/utsname.h>
#include <unistd.h>

/* Linux dma-heap UAPI, kept local for older NDK sysroots. */
struct heap_allocation_data {
    uint64_t len;
    uint32_t fd;
    uint32_t fd_flags;
    uint64_t heap_flags;
};
#define HEAP_IOCTL_ALLOC _IOWR('H', 0, struct heap_allocation_data)
#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)
#define EXPECTED_DEVICE "Mali-G2-Ultra-NX MC16"

static bool has_extension(const VkExtensionProperties *p, uint32_t n, const char *name) {
    for (uint32_t i = 0; i < n; ++i)
        if (!strcmp(p[i].extensionName, name)) return true;
    return false;
}

static bool query_heap(VkDevice device, PFN_vkGetMemoryFdPropertiesKHR query,
                       const char *path, uint64_t size, uint32_t coherent_mask) {
    int heap = open(path, O_RDONLY | O_CLOEXEC);
    if (heap < 0) {
        LOG("RESULT heap=%s SKIP open errno=%d %s; no substitution", path, errno, strerror(errno));
        return false;
    }
    struct heap_allocation_data allocation = {
        .len = size, .fd_flags = O_RDWR | O_CLOEXEC, .heap_flags = 0
    };
    if (ioctl(heap, HEAP_IOCTL_ALLOC, &allocation) < 0) {
        int error = errno;
        close(heap);
        LOG("RESULT heap=%s FAIL allocation errno=%d %s; no substitution", path, error, strerror(error));
        return false;
    }
    close(heap);
    int fd = (int)allocation.fd;
    VkMemoryFdPropertiesKHR properties = {.sType = VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR};
    VkResult result = query(device, VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, fd, &properties);
    close(fd);
    if (result != VK_SUCCESS) {
        LOG("RESULT heap=%s FAIL fd-properties result=%d; resourceCompatibility=UNKNOWN", path, result);
        return false;
    }
    LOG("RESULT heap=%s QUERY_OK bytes=%" PRIu64 " fdBits=0x%08x supports8=%u supports14=%u coherentTypeMask=0x%08x fdCoherentIntersection=0x%08x resourceCompatibility=UNKNOWN import=NOT_TESTED coherenceExecution=NOT_TESTED",
        path, size, properties.memoryTypeBits, !!(properties.memoryTypeBits & (1u << 8)),
        !!(properties.memoryTypeBits & (1u << 14)), coherent_mask,
        properties.memoryTypeBits & coherent_mask);
    return true;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    struct utsname system;
    if (!uname(&system)) LOG("SYSTEM %s %s %s", system.sysname, system.release, system.machine);
    LOG("PROBE mali-heap-query-v1 commit=%s VulkanHeaders=%u AndroidAPI=%d", PROBE_REVISION, VK_HEADER_VERSION, __ANDROID_API__);
    LOG("SAFETY temporary heap allocation and fd properties only; no resources/import/bind/mapping/data access/queue use; driver internals may run during initialization and allocation");
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0 || page > 65536 || (page & (page - 1))) {
        LOG("RESULT FAIL invalid page size=%ld; bounded maximum=65536", page);
        return 1;
    }
    uint64_t size = page > 4096 ? (uint64_t)page : 4096;
    uint32_t version = VK_API_VERSION_1_0;
    PFN_vkEnumerateInstanceVersion get_version = (PFN_vkEnumerateInstanceVersion)
        vkGetInstanceProcAddr(VK_NULL_HANDLE, "vkEnumerateInstanceVersion");
    if (!get_version || get_version(&version) != VK_SUCCESS || version < VK_API_VERSION_1_1) {
        LOG("RESULT FAIL Vulkan 1.1 required");
        return 1;
    }
    VkApplicationInfo application = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "mali-heap-query", .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo instance_info = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &application};
    VkInstance instance = VK_NULL_HANDLE;
    VkResult result = vkCreateInstance(&instance_info, NULL, &instance);
    if (result != VK_SUCCESS) { LOG("RESULT FAIL instance=%d", result); return 1; }
    int status = 1;
    VkDevice device = VK_NULL_HANDLE;
    uint32_t count = 0;
    result = vkEnumeratePhysicalDevices(instance, &count, NULL);
    if (result != VK_SUCCESS || !count || count > 16) {
        LOG("RESULT FAIL device count result=%d count=%u", result, count);
        goto done;
    }
    VkPhysicalDevice devices[16];
    result = vkEnumeratePhysicalDevices(instance, &count, devices);
    if (result != VK_SUCCESS) { LOG("RESULT FAIL enumerate devices=%d", result); goto done; }
    VkPhysicalDevice selected = VK_NULL_HANDLE;
    uint32_t matches = 0;
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties properties;
        vkGetPhysicalDeviceProperties(devices[i], &properties);
        LOG("DEVICE name=%s vendor=0x%x driver=0x%x api=%u.%u.%u", properties.deviceName,
            properties.vendorID, properties.driverVersion, VK_API_VERSION_MAJOR(properties.apiVersion),
            VK_API_VERSION_MINOR(properties.apiVersion), VK_API_VERSION_PATCH(properties.apiVersion));
        if (!strcmp(properties.deviceName, EXPECTED_DEVICE) && properties.vendorID == 0x13b5 &&
            properties.apiVersion >= VK_API_VERSION_1_1) { selected = devices[i]; ++matches; }
    }
    if (matches != 1) { LOG("RESULT FAIL expected exact Mali device matches=%u; no fallback", matches); goto done; }
    VkPhysicalDeviceMemoryProperties memory;
    vkGetPhysicalDeviceMemoryProperties(selected, &memory);
    uint32_t coherent_mask = 0;
    for (uint32_t i = 0; i < memory.memoryTypeCount; ++i) {
        VkMemoryPropertyFlags flags = memory.memoryTypes[i].propertyFlags;
        if (flags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) coherent_mask |= 1u << i;
        LOG("TYPE index=%u heap=%u flags=0x%x HOST_VISIBLE=%u HOST_COHERENT=%u HOST_CACHED=%u", i,
            memory.memoryTypes[i].heapIndex, flags, !!(flags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT),
            !!(flags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT), !!(flags & VK_MEMORY_PROPERTY_HOST_CACHED_BIT));
    }
    uint32_t extension_count = 0;
    result = vkEnumerateDeviceExtensionProperties(selected, NULL, &extension_count, NULL);
    if (result != VK_SUCCESS || !extension_count || extension_count > 4096) {
        LOG("RESULT FAIL extension count result=%d count=%u", result, extension_count); goto done;
    }
    VkExtensionProperties *extensions = calloc(extension_count, sizeof(*extensions));
    if (!extensions) { LOG("RESULT FAIL extension storage"); goto done; }
    result = vkEnumerateDeviceExtensionProperties(selected, NULL, &extension_count, extensions);
    bool supported = result == VK_SUCCESS &&
        has_extension(extensions, extension_count, VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME) &&
        has_extension(extensions, extension_count, VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
    free(extensions);
    if (!supported) { LOG("RESULT FAIL required fd and dma-buf extensions unavailable result=%d", result); goto done; }
    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, NULL);
    if (!queue_count || queue_count > 256) { LOG("RESULT FAIL queue family count=%u", queue_count); goto done; }
    VkQueueFamilyProperties *families = calloc(queue_count, sizeof(*families));
    if (!families) { LOG("RESULT FAIL queue family storage"); goto done; }
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, families);
    uint32_t family = 0;
    while (family < queue_count && !families[family].queueCount) ++family;
    free(families);
    if (family == queue_count) { LOG("RESULT FAIL no valid device queue family"); goto done; }
    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    const char *enabled[] = {VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME, VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME};
    VkDeviceCreateInfo device_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue_info,
        .enabledExtensionCount = 2, .ppEnabledExtensionNames = enabled};
    LOG("CREATE_DEVICE family=%u queue created only for valid initialization, never obtained/used; resourceCompatibility=UNKNOWN", family);
    result = vkCreateDevice(selected, &device_info, NULL, &device);
    if (result != VK_SUCCESS) { LOG("RESULT FAIL device=%d", result); goto done; }
    PFN_vkGetMemoryFdPropertiesKHR query = (PFN_vkGetMemoryFdPropertiesKHR)
        vkGetDeviceProcAddr(device, "vkGetMemoryFdPropertiesKHR");
    if (!query) { LOG("RESULT FAIL fd query function absent"); goto done; }
    bool system_ok = query_heap(device, query, "/dev/dma_heap/system", size, coherent_mask);
    bool uncached_ok = query_heap(device, query, "/dev/dma_heap/system-uncached", size, coherent_mask);
    status = system_ok && uncached_ok ? 0 : 2;
done:
    if (device != VK_NULL_HANDLE) vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);
    LOG("PROBE_DONE status=%d; QUERY_OK means fd properties obtained, not resource/import/coherence success", status);
    return status;
}
