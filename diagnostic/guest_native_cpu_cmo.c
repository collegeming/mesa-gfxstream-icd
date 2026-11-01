#define _POSIX_C_SOURCE 200809L
#ifndef CROSSVIEW_CPU_ONLY
#include <vulkan/vulkan.h>
#endif
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifndef CROSSVIEW_CPU_ONLY
#include <signal.h>
#include <setjmp.h>
#include <pthread.h>
#endif

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

/* Actions tests this exact helper on ordinary arrays, with no Vulkan linkage.
 * Only two payload sites; checks precede pointer formation and every access. */
static int cpu_crossview_words(void *base, size_t extent, bool verify_reply) {
    uintptr_t start = (uintptr_t)base;
    if (!base || extent != REQUESTED_SIZE || start % _Alignof(uint32_t) != 0 ||
        start > UINTPTR_MAX - (REQUESTED_SIZE - 1u) ||
        4092u > extent || sizeof(uint32_t) > extent - 4092u) {
        LOG("RESULT stage=word-bounds FAIL exit=20; no payload accessed"); return 20;
    }
    volatile uint32_t *first = (volatile uint32_t *)base;
    volatile uint32_t *last = (volatile uint32_t *)((unsigned char *)base + 4092u);
    if (!verify_reply) {
        LOG("BEGIN stage=request-first-write offset=0 width=4 marker=0x13579bdf");
        *first = UINT32_C(0x13579bdf);
        LOG("BEGIN stage=request-last-write offset=4092 width=4 marker=0x2468ace0");
        *last = UINT32_C(0x2468ace0);
        return 0;
    }
    LOG("BEGIN stage=reply-first-read offset=0 width=4 expected=0x89abcdef");
    uint32_t first_value = *first;
    LOG("BEGIN stage=reply-last-read offset=4092 width=4 expected=0xfedcba98");
    uint32_t last_value = *last;
    bool match = first_value == UINT32_C(0x89abcdef) && last_value == UINT32_C(0xfedcba98);
    LOG("RESULT stage=reply-verify %s first=0x%08" PRIx32 " last=0x%08" PRIx32,
        match ? "PASS" : "FAIL", first_value, last_value);
    return match ? 0 : 21;
}


#ifndef CROSSVIEW_CPU_ONLY
/* Executable local-exec TLS; never a dynamically loaded signal-handler library. */
static _Thread_local sigjmp_buf cmo_jump;
static _Thread_local volatile sig_atomic_t cmo_armed;
static _Thread_local volatile sig_atomic_t cmo_signal;
static void cmo_signal_handler(int signal_number) {
    if (!cmo_armed) _Exit(128 + signal_number);
    cmo_signal = signal_number;
    siglongjmp(cmo_jump, 1);
}

/* sigaction is process-wide; unrelated worker faults fail closed, never jump
 * into another thread. Calling-thread masks protect install/restore and target
 * arming. No Vulkan API executes under this guard. */
static int cpu_cmo(void *base, size_t extent) {
    uintptr_t start = (uintptr_t)base;
    if (!base || extent != REQUESTED_SIZE || start % _Alignof(uint32_t) != 0 ||
        start > UINTPTR_MAX - (REQUESTED_SIZE - 1u) ||
        start % REQUESTED_SIZE != 0) {
        LOG("RESULT stage=cmo-bounds FAIL exit=23 no cache operation"); return 23;
    }
#if defined(__aarch64__)
    void *last = (unsigned char *)base + 4092u;
    const int signals[3] = {SIGILL, SIGBUS, SIGSEGV};
    struct sigaction action = {0}, previous[3];
    sigset_t blocked, original;
    action.sa_handler = cmo_signal_handler;
    if (sigemptyset(&blocked) != 0 || sigaddset(&blocked, SIGILL) != 0 ||
        sigaddset(&blocked, SIGBUS) != 0 || sigaddset(&blocked, SIGSEGV) != 0 ||
        sigemptyset(&action.sa_mask) != 0) {
        LOG("RESULT stage=cmo-signal-setup FAIL exit=24"); return 24;
    }
    action.sa_mask = blocked;
    cmo_armed = 0;
    cmo_signal = 0;
    LOG("BEGIN stage=cmo-guard operation=dsb-sy,civac-at0,civac-at4092,dsb-sy attempts=1; no cache line walk; signals cannot contain kernel hangs");
    if (pthread_sigmask(SIG_BLOCK, &blocked, &original) != 0) {
        LOG("RESULT stage=cmo-signal-block FAIL exit=24"); return 24;
    }
    /* Do not temporarily unblock a signal the caller deliberately blocked. */
    if (sigismember(&original, SIGILL) != 0 || sigismember(&original, SIGBUS) != 0 ||
        sigismember(&original, SIGSEGV) != 0) {
        if (pthread_sigmask(SIG_SETMASK, &original, NULL) != 0) _Exit(25);
        LOG("RESULT stage=cmo-original-mask FAIL exit=24"); return 24;
    }
    unsigned installed = 0;
    for (; installed < 3; ++installed) {
        if (sigaction(signals[installed], &action, &previous[installed]) != 0) break;
    }
    if (installed != 3) {
        while (installed) {
            --installed;
            if (sigaction(signals[installed], &previous[installed], NULL) != 0) _Exit(25);
        }
        if (pthread_sigmask(SIG_SETMASK, &original, NULL) != 0) _Exit(25);
        LOG("RESULT stage=cmo-handler-install FAIL exit=24"); return 24;
    }
    cmo_signal = 0;
    volatile sig_atomic_t status = 0;
    if (sigsetjmp(cmo_jump, 1) == 0) {
        cmo_armed = 1;
        if (pthread_sigmask(SIG_SETMASK, &original, NULL) != 0) status = 24;
        else {
            __asm__ volatile("dsb sy\n\tdc civac, %0\n\tdc civac, %1\n\tdsb sy"
                             : : "r"(base), "r"(last) : "memory");
        }
    } else status = 26;
    /* siglongjmp restores the saved blocked mask. Block again on success too. */
    if (pthread_sigmask(SIG_BLOCK, &blocked, NULL) != 0) _Exit(25);
    cmo_armed = 0;
    for (unsigned i = 0; i < 3; ++i) {
        if (sigaction(signals[i], &previous[i], NULL) != 0) _Exit(25);
    }
    if (pthread_sigmask(SIG_SETMASK, &original, NULL) != 0) _Exit(25);
    LOG("RESULT stage=cmo-guard %s signal=%d exit=%d; failure skips flush/reply, no retry; operation completion is not coherence proof",
        status == 0 ? "COMPLETED" : "FAIL", (int)cmo_signal, (int)status);
    return (int)status;
#else
    LOG("RESULT stage=cmo-architecture UNSUPPORTED exit=27; no substitute operation");
    return 27;
#endif
}
#endif

#ifndef CROSSVIEW_CPU_ONLY
/* Offline delivery only. Parent reviews guest and existing host source; CLI is a technical interlock.
 * CPU cross-view interlock only, NOT GPU user consent. No automatic retry. */
int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "--allow-cpu-cmo") != 0) {
        LOG("RESULT stage=cli FAIL exit=2 usage: guest-native-cpu-cmo --allow-cpu-cmo; flag is NOT GPU user consent; no Vulkan initialized");
        LOG("CAUTION first payload access may hang kernel/reboot host and lose root; timeout cannot guarantee recovery; no automatic retry. Parent source review required; testing authorization is managed by parent; this CLI is only a technical interlock.");
        return 2;
    }
    LOG("PROBE guest-native-cpu-cmo-v1 commit=%s VulkanHeaders=%u", PROBE_REVISION, VK_HEADER_VERSION);
    LOG("BOUNDARY flag is NOT GPU user consent; parent source safety review required before any device action");
    LOG("BOUNDARY no application GPU workload; Vulkan initialization/allocation/mapping may cause driver-internal GPU activity");
    LOG("BOUNDARY exactly two request writes, two VA CIVAC operations with DSB SY, one synchronous flush, two reply reads at0/4092; no resources/bindings/commands/shaders/queue retrieval/submission; no manual mapping or KVM");
    LOG("HOST_CONTRACT requires GFXSTREAM_NATIVE_CPU_CROSSVIEW=1 in host wrapper inherited environment; guest never sets host environment; owner/current epoch/no bound resources/request-match checks before host writes replies");
    LOG("LIMIT explicit maintenance diagnostic, NOT production coherent fix or FWB proof; requested size only: driver internal real blob may be larger; no actual blob extent, coherence, backing identity or renderer achievement claimed");
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
        .pNext = NULL, .pApplicationName = "guest-native-cpu-crossview", .apiVersion = VK_API_VERSION_1_1};
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
    status = cpu_crossview_words(mapped_pointer, (size_t)size, false);
    if (status != 0) goto done;
    status = cpu_cmo(mapped_pointer, (size_t)size);
    if (status != 0) goto done;
    VkMappedMemoryRange range = {.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
        .pNext = NULL, .memory = allocation, .offset = 0, .size = size};
    status = 22;
    LOG("BEGIN stage=flush offset=0 size=4096 count=1 pNext=NULL; synchronous diagnostic handshake, no GPU submission");
    result = vkFlushMappedMemoryRanges(device, 1, &range);
    LOG("RESULT stage=flush %s VkResult=%d; no retry; API error means no reply reads", result == VK_SUCCESS ? "PASS" : "FAIL", result);
    if (result != VK_SUCCESS) goto done;
    status = cpu_crossview_words(mapped_pointer, (size_t)size, true);
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
    LOG("RESULT stage=done %s exit=%d; CPU cross-view only, NOT HOST_COHERENT or GPU coherence proof; no root first-fault, backing identity or renderer achievement proof", status == 0 ? "PASS" : "FAIL", status);
    return status;
}
#endif
