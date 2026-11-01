#define _POSIX_C_SOURCE 200809L
#ifndef ROUNDTRIP_CPU_ONLY
#include <vulkan/vulkan.h>
#endif
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
#define BUFFER_SIZE 4096u
#define MAX_ALLOCATION 65536u
#define WORDS 64u
#define COPY_BYTES 256u
#define MAX_DEVICES 16u
#define MAX_FAMILIES 256u
#define EXPECTED_ICD "/opt/droidvm-mesa-reference/icd.json"
#define EXPECTED_LIBRARY_PREFIX "/opt/droidvm-mesa-reference/usr/local/lib/aarch64-linux-gnu"
#define LOG(...) do { printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)

/* These exact helpers are tested on ordinary arrays in Actions, without Vulkan. */
static uint32_t pattern(uint32_t i) {
    return UINT32_C(0x13579bdf) ^ (UINT32_C(0x01020408) * (i + 1u));
}
static bool payload_bounds(const void *base, size_t extent) {
    return base && extent == BUFFER_SIZE && (uintptr_t)base % _Alignof(uint32_t) == 0 &&
        (uintptr_t)base <= UINTPTR_MAX - COPY_BYTES;
}
static int prepare_payload(void *source, void *destination, size_t extent) {
    if (!payload_bounds(source, extent) || !payload_bounds(destination, extent) ||
        !((uintptr_t)source + COPY_BYTES <= (uintptr_t)destination ||
          (uintptr_t)destination + COPY_BYTES <= (uintptr_t)source)) return 20;
    volatile uint32_t *src = source, *dst = destination;
    for (uint32_t i = 0; i < WORDS; ++i) { src[i] = pattern(i); dst[i] = ~pattern(i); }
    return 0;
}
static int verify_payload(const void *destination, size_t extent) {
    if (!payload_bounds(destination, extent)) return 20;
    const volatile uint32_t *dst = destination;
    uint32_t mismatches = 0;
    for (uint32_t i = 0; i < WORDS; ++i) {
        uint32_t value = dst[i];
        if (value != pattern(i)) {
            LOG("MISMATCH word=%u expected=0x%08" PRIx32 " actual=0x%08" PRIx32, i, pattern(i), value);
            ++mismatches;
        }
    }
    LOG("RESULT stage=compare %s words=64 mismatches=%u", mismatches ? "FAIL" : "PASS", mismatches);
    return mismatches ? 21 : 0;
}

#ifndef ROUNDTRIP_CPU_ONLY
/* Never exit/tear down uncertain in-flight work. External fatal signals/process
 * death remain unsafe: parent must stop the VM, NOT terminate this probe first.
 * Quarantine is not recovery from a wedged kernel/host GPU. No timeout guarantee. */
static _Noreturn void quarantine(int reason, VkResult result) {
    LOG("RESULT stage=quarantine FAIL reason=%d VkResult=%d exit=none GPU_COMPLETED=0 resources=retained; STOP VM externally; do NOT kill probe or retry; kernel/host hang remains possible", reason, result);
    for (;;) pause();
}
#define CHECK(stage, call) do { \
    LOG("BEGIN stage=%s", stage); result = (call); \
    LOG("RESULT stage=%s %s VkResult=%d", stage, result == VK_SUCCESS ? "PASS" : "FAIL", result); \
    if (result == VK_ERROR_DEVICE_LOST) quarantine(42, result); \
    if (result != VK_SUCCESS) goto done; \
} while (0)

int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "--allow-gpu-roundtrip") != 0) {
        LOG("RESULT stage=cli FAIL exit=2; require --allow-gpu-roundtrip; no Vulkan initialized; flag is NOT user consent"); return 2;
    }
    LOG("PROBE guest-native-buffer-roundtrip-v1 commit=%s VulkanHeaders=%u base=22ac42cb9e28e77068658a81c418245aaef1571e", PROBE_REVISION, VK_HEADER_VERSION);
    LOG("BOUNDARY separate parent GPU authorization and source safety review REQUIRED; prior CPU first-touch approval is NOT GPU consent");
    LOG("BOUNDARY two buffers4096, two ordinary allocations<=65536 each type8, copy256 only, one submit, one fence wait1000000000ns; no fill/images/shaders/draw/WSI/import/export/manual mapping");
    LOG("CAUTION timeout cannot guarantee recovery; driver calls may hang; pending/error quarantine requires parent VM stop; no automatic retry; no host backing identity/coherence proof beyond this transfer");
    const char *icd = getenv("VK_DRIVER_FILES"), *libraries = getenv("LD_LIBRARY_PATH");
    const char *legacy_icd = getenv("VK_ICD_FILENAMES"), *layers = getenv("VK_INSTANCE_LAYERS"), *preload = getenv("LD_PRELOAD");
    size_t prefix_size = sizeof(EXPECTED_LIBRARY_PREFIX) - 1;
    if (!icd || strcmp(icd, EXPECTED_ICD) != 0 || !libraries ||
        strncmp(libraries, EXPECTED_LIBRARY_PREFIX, prefix_size) != 0 ||
        (libraries[prefix_size] != '\0' && libraries[prefix_size] != ':') ||
        (legacy_icd && legacy_icd[0]) || (layers && layers[0]) || (preload && preload[0])) {
        LOG("RESULT stage=environment FAIL exit=3; strict isolated Mesa selectors required; no Vulkan initialized"); return 3;
    }
    LOG("RESULT stage=environment PASS ICD=%s libraryPath=%s; selectors are NOT proof of loaded driver identity", icd, libraries);
    long page = sysconf(_SC_PAGESIZE);
    if (page != 4096) { LOG("RESULT stage=page FAIL exit=4 page=%ld require4096", page); return 4; }
    int status = 10;
    VkResult result;
    VkInstance instance = VK_NULL_HANDLE;
    VkDevice device = VK_NULL_HANDLE;
    VkBuffer buffers[2] = {VK_NULL_HANDLE, VK_NULL_HANDLE};
    VkDeviceMemory memory[2] = {VK_NULL_HANDLE, VK_NULL_HANDLE};
    void *mapped[2] = {NULL, NULL};
    bool is_mapped[2] = {false, false};
    VkCommandPool pool = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "guest-native-buffer-roundtrip", .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    CHECK("instance", vkCreateInstance(&ici, NULL, &instance));
    uint32_t count = 0;
    CHECK("enumerate-count", vkEnumeratePhysicalDevices(instance, &count, NULL));
    if (!count || count > MAX_DEVICES) goto done;
    VkPhysicalDevice devices[MAX_DEVICES], selected = VK_NULL_HANDLE;
    CHECK("enumerate-list", vkEnumeratePhysicalDevices(instance, &count, devices));
    if (!count || count > MAX_DEVICES) goto done;
    VkPhysicalDeviceProperties properties = {0};
    uint32_t matches = 0;
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties p;
        vkGetPhysicalDeviceProperties(devices[i], &p);
        LOG("DEVICE index=%u name=%s vendor=0x%x device=0x%x driver=0x%x api=0x%x", i, p.deviceName, p.vendorID, p.deviceID, p.driverVersion, p.apiVersion);
        if ((strstr(p.deviceName, "Virtio-GPU") || strstr(p.deviceName, "GFXStream")) &&
            strstr(p.deviceName, "Mali") && p.apiVersion >= VK_API_VERSION_1_1) {
            selected = devices[i]; properties = p; ++matches;
        }
    }
    if (matches != 1) { LOG("RESULT stage=identity FAIL matches=%u no fallback", matches); goto done; }
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(selected, &mp);
    if (mp.memoryTypeCount <= MEMORY_TYPE_INDEX || mp.memoryTypeCount > VK_MAX_MEMORY_TYPES ||
        mp.memoryHeapCount > VK_MAX_MEMORY_HEAPS) goto done;
    VkMemoryType type = mp.memoryTypes[MEMORY_TYPE_INDEX];
    VkMemoryPropertyFlags required = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    LOG("TYPE index=8 flags=0x%x heap=%u", type.propertyFlags, type.heapIndex);
    if ((type.propertyFlags & required) != required || (type.propertyFlags & VK_MEMORY_PROPERTY_PROTECTED_BIT) ||
        type.heapIndex >= mp.memoryHeapCount) goto done;
    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, NULL);
    if (!queue_count || queue_count > MAX_FAMILIES) goto done;
    VkQueueFamilyProperties families[MAX_FAMILIES];
    vkGetPhysicalDeviceQueueFamilyProperties(selected, &queue_count, families);
    if (!queue_count || queue_count > MAX_FAMILIES) goto done;
    uint32_t family = 0;
    while (family < queue_count && (!families[family].queueCount ||
        !(families[family].queueFlags & (VK_QUEUE_TRANSFER_BIT | VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)))) ++family;
    if (family == queue_count) goto done;
    LOG("RESULT stage=selection PASS family=%u type=8 no substitute", family);
    float priority = 1.0f;
    VkDeviceQueueCreateInfo qci = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    VkDeviceCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci, .pEnabledFeatures = NULL};
    CHECK("device", vkCreateDevice(selected, &dci, NULL, &device));
    VkQueue queue = VK_NULL_HANDLE;
    vkGetDeviceQueue(device, family, 0, &queue);
    if (queue == VK_NULL_HANDLE) goto done;
    VkMemoryRequirements requirements[2];
    VkBufferCreateInfo bci = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = BUFFER_SIZE,
        .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT, .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    for (uint32_t i = 0; i < 2; ++i) {
        LOG("BUFFER index=%u requestedSize=4096", i);
        CHECK("buffer", vkCreateBuffer(device, &bci, NULL, &buffers[i]));
        vkGetBufferMemoryRequirements(device, buffers[i], &requirements[i]);
        LOG("REQUIREMENTS index=%u size=%" PRIu64 " alignment=%" PRIu64 " typeBits=0x%x", i,
            (uint64_t)requirements[i].size, (uint64_t)requirements[i].alignment, requirements[i].memoryTypeBits);
        if (requirements[i].size < BUFFER_SIZE || requirements[i].size > MAX_ALLOCATION ||
            !requirements[i].alignment || (requirements[i].alignment & (requirements[i].alignment - 1)) ||
            requirements[i].size % requirements[i].alignment != 0) goto done;
    }
    if (!(requirements[0].memoryTypeBits & requirements[1].memoryTypeBits & (1u << MEMORY_TYPE_INDEX)) ||
        requirements[0].size + requirements[1].size > mp.memoryHeaps[type.heapIndex].size) goto done;
    for (uint32_t i = 0; i < 2; ++i) {
        VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
            .pNext = NULL, .allocationSize = requirements[i].size, .memoryTypeIndex = MEMORY_TYPE_INDEX};
        LOG("ALLOCATION index=%u size=%" PRIu64 " type=8 pNext=NULL bindOffset=0", i, (uint64_t)ai.allocationSize);
        CHECK("allocate", vkAllocateMemory(device, &ai, NULL, &memory[i]));
        CHECK("bind", vkBindBufferMemory(device, buffers[i], memory[i], 0));
        CHECK("map", vkMapMemory(device, memory[i], 0, BUFFER_SIZE, 0, &mapped[i]));
        is_mapped[i] = true;
        uintptr_t address = (uintptr_t)mapped[i];
        size_t alignment = properties.limits.minMemoryMapAlignment;
        LOG("ADDRESS index=%u address=0x%" PRIxPTR " minMapAlignment=%zu", i, address, alignment);
        if (!alignment || address % alignment || !payload_bounds(mapped[i], BUFFER_SIZE)) goto done;
    }
    LOG("BEGIN stage=cpu-upload source64words destination64distinctwords bytesEach=256");
    status = prepare_payload(mapped[0], mapped[1], BUFFER_SIZE);
    if (status) goto done;
    status = 11;
    VkCommandPoolCreateInfo pci = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT, .queueFamilyIndex = family};
    CHECK("command-pool", vkCreateCommandPool(device, &pci, NULL, &pool));
    VkCommandBuffer command = VK_NULL_HANDLE;
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    CHECK("command-buffer", vkAllocateCommandBuffers(device, &cai, &command));
    VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    CHECK("begin", vkBeginCommandBuffer(command, &begin));
    VkMemoryBarrier upload = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_HOST_WRITE_BIT,
        .dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT};
    LOG("BEGIN stage=record-host-transfer-barrier");
    vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_HOST_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &upload, 0, NULL, 0, NULL);
    VkBufferCopy copy = {.srcOffset = 0, .dstOffset = 0, .size = COPY_BYTES};
    LOG("BEGIN stage=record-copy bytes=256");
    vkCmdCopyBuffer(command, buffers[0], buffers[1], 1, &copy);
    VkMemoryBarrier download = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT};
    LOG("BEGIN stage=record-transfer-host-barrier");
    vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &download, 0, NULL, 0, NULL);
    CHECK("end", vkEndCommandBuffer(command));
    VkFenceCreateInfo fci = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    CHECK("fence", vkCreateFence(device, &fci, NULL, &fence));
    VkSubmitInfo submit = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &command};
    LOG("BEGIN stage=submit attempts=1; any non-success retains all resources");
    result = vkQueueSubmit(queue, 1, &submit, fence);
    LOG("RESULT stage=submit VkResult=%d", result);
    if (result != VK_SUCCESS) quarantine(40, result);
    LOG("BEGIN stage=fence-wait timeoutNs=1000000000 attempts=1");
    result = vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_C(1000000000));
    LOG("RESULT stage=fence-wait VkResult=%d", result);
    if (result != VK_SUCCESS) quarantine(41, result);
    LOG("RESULT stage=gpu-completion PASS GPU_COMPLETED=1 fence=VK_SUCCESS; only this transfer, not general GPU acceptance");
    LOG("BEGIN stage=cpu-readback words=64 bytes=256");
    status = verify_payload(mapped[1], BUFFER_SIZE);
done:
    /* Reached only before submit or after a successful fence. No idle waits. */
    LOG("BEGIN stage=cleanup status=%d; no pending submission on this path", status);
    if (fence != VK_NULL_HANDLE) vkDestroyFence(device, fence, NULL);
    if (pool != VK_NULL_HANDLE) vkDestroyCommandPool(device, pool, NULL);
    for (uint32_t i = 0; i < 2; ++i) {
        if (is_mapped[i]) vkUnmapMemory(device, memory[i]);
        if (buffers[i] != VK_NULL_HANDLE) vkDestroyBuffer(device, buffers[i], NULL);
        if (memory[i] != VK_NULL_HANDLE) vkFreeMemory(device, memory[i], NULL);
    }
    if (device != VK_NULL_HANDLE) vkDestroyDevice(device, NULL);
    if (instance != VK_NULL_HANDLE) vkDestroyInstance(instance, NULL);
    LOG("RESULT stage=done %s exit=%d; no backing identity or kernel safety guarantee", status == 0 ? "PASS" : "FAIL", status);
    return status;
}
#endif
