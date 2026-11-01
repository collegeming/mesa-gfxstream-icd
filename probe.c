#define _GNU_SOURCE
#include <dlfcn.h>
#include <execinfo.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef void *VkInstance;
typedef void *VkPhysicalDevice;
typedef void *VkDevice;
typedef void *VkBuffer;
typedef void *VkDeviceMemory;
typedef void *VkQueue;

typedef struct {
    int sType;
    const void *pNext;
    const char *pApplicationName;
    uint32_t applicationVersion;
    const char *pEngineName;
    uint32_t engineVersion;
    uint32_t apiVersion;
} AppInfo;

typedef struct {
    int sType;
    const void *pNext;
    uint32_t flags;
    const AppInfo *pApplicationInfo;
    uint32_t enabledLayerCount;
    const char *const *ppEnabledLayerNames;
    uint32_t enabledExtensionCount;
    const char *const *ppEnabledExtensionNames;
} InstanceCreateInfo;

typedef struct {
    uint32_t queueFlags;
    uint32_t queueCount;
    uint32_t timestampValidBits;
    uint32_t minImageTransferGranularity[3];
} QueueFamilyProperties;

typedef struct {
    int sType;
    const void *pNext;
    uint32_t flags;
    uint32_t queueFamilyIndex;
    uint32_t queueCount;
    const float *pQueuePriorities;
} DeviceQueueCreateInfo;

typedef struct {
    int sType;
    const void *pNext;
    uint32_t flags;
    uint32_t queueCreateInfoCount;
    const DeviceQueueCreateInfo *pQueueCreateInfos;
    uint32_t enabledLayerCount;
    const char *const *ppEnabledLayerNames;
    uint32_t enabledExtensionCount;
    const char *const *ppEnabledExtensionNames;
    const void *pEnabledFeatures;
} DeviceCreateInfo;

typedef struct {
    int sType;
    const void *pNext;
    uint32_t flags;
    uint64_t size;
    uint32_t usage;
    uint32_t sharingMode;
    uint32_t queueFamilyIndexCount;
    const uint32_t *pQueueFamilyIndices;
} BufferCreateInfo;

typedef struct {
    uint64_t size;
    uint64_t alignment;
    uint32_t memoryTypeBits;
} MemoryRequirements;

typedef struct {
    int sType;
    const void *pNext;
    uint64_t allocationSize;
    uint32_t memoryTypeIndex;
} MemoryAllocateInfo;

typedef struct {
    uint32_t propertyFlags;
    uint32_t heapIndex;
} MemoryType;

typedef struct {
    uint64_t size;
    uint32_t flags;
} MemoryHeap;

typedef struct {
    uint32_t memoryTypeCount;
    MemoryType memoryTypes[32];
    uint32_t memoryHeapCount;
    MemoryHeap memoryHeaps[16];
} PhysicalDeviceMemoryProperties;

typedef struct {
    uint32_t apiVersion;
    uint32_t driverVersion;
    uint32_t vendorID;
    uint32_t deviceID;
    int deviceType;
    char deviceName[256];
} PhysicalDeviceProperties;

static void segv(int sig) {
    void *bt[64];
    char msg[] = "\n=== BACKTRACE ===\n";
    int n = backtrace(bt, 64);
    write(2, msg, sizeof(msg) - 1);
    backtrace_symbols_fd(bt, n, 2);
    signal(sig, SIG_DFL);
    raise(sig);
}

#define P(...) do { fprintf(stderr, __VA_ARGS__); fprintf(stderr, "\n"); fflush(stderr); } while (0)

int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "/opt/gfxicd/libvulkan_gfxstream.so";
    signal(SIGSEGV, segv);
    signal(SIGABRT, segv);
    signal(SIGBUS, segv);

    void *lib = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!lib) { P("dlopen failed: %s", dlerror()); return 1; }
    typedef void *(*GIPA)(void *, const char *);
    GIPA gipa = (GIPA)dlsym(lib, "vk_icdGetInstanceProcAddr");
    if (!gipa) { P("no vk_icdGetInstanceProcAddr"); return 1; }

    typedef int (*NEG)(uint32_t *);
    NEG neg = (NEG)dlsym(lib, "vk_icdNegotiateLoaderICDInterfaceVersion");
    if (neg) { uint32_t v = 5; int r = neg(&v); P("negotiate -> %d v%u", r, v); }

    typedef int (*CREATEI)(const InstanceCreateInfo *, void *, VkInstance *);
    CREATEI create_instance = (CREATEI)gipa(NULL, "vkCreateInstance");
    AppInfo ai = {0, NULL, "cprobe", 1, "none", 1, (1u << 22) | (3u << 12)};
    InstanceCreateInfo ci = {1, NULL, 0, &ai, 0, NULL, 0, NULL};
    VkInstance inst = NULL;
    P("create_instance ptr=%p", (void *)create_instance);
    int r = create_instance(&ci, NULL, &inst);
    P("create instance -> %d %p", r, inst);
    if (r) return 1;

    typedef int (*ENUMD)(VkInstance, uint32_t *, VkPhysicalDevice *);
    ENUMD enumd = (ENUMD)gipa(inst, "vkEnumeratePhysicalDevices");
    uint32_t n = 0;
    enumd(inst, &n, NULL);
    VkPhysicalDevice *devs = calloc(n, sizeof(*devs));
    enumd(inst, &n, devs);
    VkPhysicalDevice pd = devs[0];
    P("devices: %u", n);

    typedef void (*GETPROPS)(VkPhysicalDevice, PhysicalDeviceProperties *);
    PhysicalDeviceProperties props;
    memset(&props, 0, sizeof(props));
    GETPROPS getprops = (GETPROPS)gipa(inst, "vkGetPhysicalDeviceProperties");
    getprops(pd, &props);
    P("device: %s", props.deviceName);

    typedef void (*GETQF)(VkPhysicalDevice, uint32_t *, QueueFamilyProperties *);
    GETQF getqf = (GETQF)gipa(inst, "vkGetPhysicalDeviceQueueFamilyProperties");
    uint32_t qn = 0;
    getqf(pd, &qn, NULL);
    QueueFamilyProperties *qf = calloc(qn, sizeof(*qf));
    getqf(pd, &qn, qf);
    P("queue families: %u flags=%x count=%u", qn, qf[0].queueFlags, qf[0].queueCount);

    float prio = 1.0f;
    DeviceQueueCreateInfo qci = {2, NULL, 0, 0, 1, &prio};
    DeviceCreateInfo dci = {3, NULL, 0, 1, &qci, 0, NULL, 0, NULL, NULL};
    typedef int (*CREATED)(VkPhysicalDevice, const DeviceCreateInfo *, void *, VkDevice *);
    CREATED created = (CREATED)gipa(inst, "vkCreateDevice");
    VkDevice dev = NULL;
    r = created(pd, &dci, NULL, &dev);
    P("create device -> %d %p", r, dev);
    if (r) return 1;

    typedef void *(*GDPA)(VkDevice, const char *);
    GDPA gdpa = (GDPA)gipa(inst, "vkGetDeviceProcAddr");

    BufferCreateInfo bci = {12, NULL, 0, 4096, 0x20 | 0x2, 0, 0, NULL};
    typedef int (*CREATEB)(VkDevice, const BufferCreateInfo *, void *, VkBuffer *);
    CREATEB createb = (CREATEB)gdpa(dev, "vkCreateBuffer");
    VkBuffer buf = NULL;
    r = createb(dev, &bci, NULL, &buf);
    P("create buffer -> %d %p", r, buf);

    typedef void (*GETREQ)(VkDevice, VkBuffer, MemoryRequirements *);
    MemoryRequirements req;
    GETREQ getreq = (GETREQ)gdpa(dev, "vkGetBufferMemoryRequirements");
    getreq(dev, buf, &req);
    P("req size=%llu bits=%x", (unsigned long long)req.size, req.memoryTypeBits);

    typedef void (*GETMEM)(VkPhysicalDevice, PhysicalDeviceMemoryProperties *);
    PhysicalDeviceMemoryProperties mp;
    GETMEM getmem = (GETMEM)gipa(inst, "vkGetPhysicalDeviceMemoryProperties");
    getmem(pd, &mp);
    int idx = -1;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++) {
        if (((req.memoryTypeBits >> i) & 1) && (mp.memoryTypes[i].propertyFlags & 0x2)) {
            idx = (int)i;
            break;
        }
    }
    P("host-visible type: %d (of %u)", idx, mp.memoryTypeCount);
    if (idx < 0) return 1;

    MemoryAllocateInfo mai = {5, NULL, req.size, (uint32_t)idx};
    typedef int (*ALLOC)(VkDevice, const MemoryAllocateInfo *, void *, VkDeviceMemory *);
    ALLOC alloc = (ALLOC)gdpa(dev, "vkAllocateMemory");
    VkDeviceMemory mem = NULL;
    r = alloc(dev, &mai, NULL, &mem);
    P("allocate -> %d %p", r, mem);
    if (r) return 1;

    typedef int (*BIND)(VkDevice, VkBuffer, VkDeviceMemory, uint64_t);
    BIND bind = (BIND)gdpa(dev, "vkBindBufferMemory");
    P("bind ptr=%p", (void *)bind);
    r = bind(dev, buf, mem, 0);
    P("bind -> %d", r);

    typedef int (*MAP)(VkDevice, VkDeviceMemory, uint64_t, uint64_t, uint32_t, void **);
    MAP map = (MAP)gdpa(dev, "vkMapMemory");
    void *ptr = NULL;
    r = map(dev, mem, 0, 4096, 0, &ptr);
    P("map -> %d ptr=%p", r, ptr);
    if (r == 0) {
        memcpy(ptr, "GPU-WORKS-12345", 16);
        P("roundtrip: %.16s", (char *)ptr);
    }
    P("CPROBE_DONE");
    return 0;
}
