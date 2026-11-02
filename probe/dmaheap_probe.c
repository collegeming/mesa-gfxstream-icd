// Mali external-memory import probe for Android hosts.
//
// Answers one question with real hardware: can this Mali driver import a dma-buf
// produced by /dev/dma_heap (the exporter gralloc and Arm's WSI layer use) as a
// VkImage, and does it behave differently from a udmabuf produced by the kernel's
// udmabuf driver? Both exporters page-back a memfd, so a difference isolates the
// exporter/dma-buf-ops as the cause of VK_ERROR_INVALID_EXTERNAL_HANDLE.
//
// Build: NDK aarch64-linux-android33-clang, link -lvulkan (Android loader).
// Run on device as root; prints PROBE-* lines and exits 0 even when imports fail.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <vulkan/vulkan.h>

#ifndef VK_EXT_external_memory_dma_buf
#define VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME "VK_EXT_external_memory_dma_buf"
#endif

struct dma_heap_allocation_data {
    uint64_t len;
    uint32_t fd;
    uint32_t fd_flags;
    uint64_t heap_flags;
};
#define DMA_HEAP_IOC_MAGIC 'H'
#define DMA_HEAP_IOCTL_ALLOC _IOWR(DMA_HEAP_IOC_MAGIC, 0, struct dma_heap_allocation_data)

struct udmabuf_create {
    uint32_t memfd;
    uint32_t flags;
    uint64_t offset;
    uint64_t size;
};
#define UDMABUF_CREATE _IOW('u', 0x42, struct udmabuf_create)
#define UDMABUF_FLAGS_CLOEXEC 0x02

#define WIDTH 1280
#define HEIGHT 720
#define STRIDE (WIDTH * 4)
#define IMAGE_SIZE ((uint64_t)STRIDE * HEIGHT)
#define USAGE (VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT | \
               VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT)

#define P(...) do { printf(__VA_ARGS__); printf("\n"); fflush(stdout); } while (0)

static uint64_t align_up(uint64_t v, uint64_t a) { return (v + a - 1) & ~(a - 1); }

static int make_heap_fd(const char *path, uint64_t size) {
    int heap = open(path, O_RDONLY | O_CLOEXEC);
    if (heap < 0) {
        P("PROBE-HEAP-OPEN %s errno=%d", path, errno);
        return -1;
    }
    struct dma_heap_allocation_data req = {0};
    req.len = size;
    req.fd_flags = O_RDWR | O_CLOEXEC;
    int fd = ioctl(heap, DMA_HEAP_IOCTL_ALLOC, &req);
    int saved = errno;
    close(heap);
    if (fd < 0) {
        P("PROBE-HEAP-ALLOC %s size=%llu errno=%d", path, (unsigned long long)size, saved);
        return -1;
    }
    P("PROBE-HEAP-ALLOC %s size=%llu fd=%d", path, (unsigned long long)size, fd);
    return fd;
}

static int make_udmabuf_fd(uint64_t size) {
    int memfd = (int)syscall(SYS_memfd_create, "probe-udmabuf", 0x0001 /* MFD_CLOEXEC */);
    if (memfd < 0) {
        P("PROBE-UDMA-MEMFD errno=%d", errno);
        return -1;
    }
    if (ftruncate(memfd, (off_t)size) != 0) {
        P("PROBE-UDMA-FTRUNCATE errno=%d", errno);
        close(memfd);
        return -1;
    }
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
    if (p == MAP_FAILED) {
        P("PROBE-UDMA-MMAP errno=%d", errno);
        close(memfd);
        return -1;
    }
    for (uint64_t off = 0; off < size; off += 4096) ((volatile char *)p)[off] = 1;
    munmap(p, size);

    int ctl = open("/dev/udmabuf", O_RDONLY | O_CLOEXEC);
    if (ctl < 0) {
        P("PROBE-UDMA-CTL errno=%d", errno);
        close(memfd);
        return -1;
    }
    struct udmabuf_create create = {0};
    create.memfd = (uint32_t)memfd;
    create.flags = UDMABUF_FLAGS_CLOEXEC;
    create.size = size;
    int fd = ioctl(ctl, UDMABUF_CREATE, &create);
    int saved = errno;
    close(ctl);
    close(memfd);
    if (fd < 0) {
        P("PROBE-UDMA-CREATE size=%llu errno=%d", (unsigned long long)size, saved);
        return -1;
    }
    P("PROBE-UDMA-CREATE size=%llu fd=%d", (unsigned long long)size, fd);
    return fd;
}

static VkInstance g_instance;
static VkPhysicalDevice g_phys;
static VkDevice g_device;
static VkQueue g_queue;
static uint32_t g_queue_family;

static int has_ext(const char *name) {
    uint32_t n = 0;
    vkEnumerateDeviceExtensionProperties(g_phys, NULL, &n, NULL);
    VkExtensionProperties *props = calloc(n, sizeof(*props));
    vkEnumerateDeviceExtensionProperties(g_phys, NULL, &n, props);
    int found = 0;
    for (uint32_t i = 0; i < n; i++) {
        if (strcmp(props[i].extensionName, name) == 0) found = 1;
    }
    free(props);
    return found;
}

static int create_image(VkImage *image, const char *tag) {
    VkSubresourceLayout plane = {0};
    plane.offset = 0;
    plane.rowPitch = STRIDE;
    plane.size = IMAGE_SIZE;

    VkImageDrmFormatModifierExplicitCreateInfoEXT explicit_mod = {0};
    explicit_mod.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
    explicit_mod.drmFormatModifier = 0;  // DRM_FORMAT_MOD_LINEAR
    explicit_mod.drmFormatModifierPlaneCount = 1;
    explicit_mod.pPlaneLayouts = &plane;

    VkExternalMemoryImageCreateInfo external = {0};
    external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
    external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    external.pNext = NULL;
    explicit_mod.pNext = &external;

    VkImageCreateInfo ci = {0};
    ci.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    ci.pNext = &explicit_mod;
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = VK_FORMAT_B8G8R8A8_UNORM;
    ci.extent.width = WIDTH;
    ci.extent.height = HEIGHT;
    ci.extent.depth = 1;
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
    ci.usage = USAGE;
    ci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VkResult r = vkCreateImage(g_device, &ci, NULL, image);
    P("PROBE-IMAGE-CREATE %s result=%d image=%p", tag, r, (void *)*image);
    return r;
}

static int import_image(int fd, VkImage image, const char *tag) {
    VkMemoryRequirements req = {0};
    vkGetImageMemoryRequirements(g_device, image, &req);
    P("PROBE-IMG-REQ %s size=%llu align=%llu bits=0x%x fd=%d", tag,
      (unsigned long long)req.size, (unsigned long long)req.alignment, req.memoryTypeBits, fd);

    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(g_phys, &mp);
    int index = -1;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++) {
        if ((req.memoryTypeBits & (1u << i)) != 0) {
            if (index < 0) index = (int)i;
            P("PROBE-MEMTYPE %s index=%u flags=0x%x heap=%u", tag, i,
              mp.memoryTypes[i].propertyFlags, mp.memoryTypes[i].heapIndex);
        }
    }
    if (index < 0) {
        P("PROBE-IMPORT %s result=NO-MEMTYPE", tag);
        return -1;
    }

    VkMemoryDedicatedAllocateInfo dedicated = {0};
    dedicated.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO;
    dedicated.image = image;

    VkImportMemoryFdInfoKHR import = {0};
    import.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR;
    import.pNext = &dedicated;
    import.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    import.fd = fd;

    VkMemoryAllocateInfo ai = {0};
    ai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    ai.pNext = &import;
    ai.allocationSize = align_up(req.size, 4096);
    ai.memoryTypeIndex = (uint32_t)index;

    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkResult r = vkAllocateMemory(g_device, &ai, NULL, &memory);
    P("PROBE-IMPORT %s result=%d memory=%p type=%d", tag, r, (void *)memory, index);
    if (r != VK_SUCCESS) return (int)r;

    r = vkBindImageMemory(g_device, image, memory, 0);
    P("PROBE-BIND %s result=%d", tag, r);
    return (int)r;
}

// Clear the imported image with Mali and read the pixels back through a normal
// host-visible buffer, so a successful import is also proven to render.
static void render_check(VkImage image, const char *tag) {
    VkBufferCreateInfo bci = {0};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.size = IMAGE_SIZE;
    bci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkBuffer buffer = VK_NULL_HANDLE;
    VkResult r = vkCreateBuffer(g_device, &bci, NULL, &buffer);
    if (r != VK_SUCCESS) { P("PROBE-RENDER %s buffer-create=%d", tag, r); return; }

    VkMemoryRequirements req = {0};
    vkGetBufferMemoryRequirements(g_device, buffer, &req);
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(g_phys, &mp);
    int index = -1;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++) {
        if ((req.memoryTypeBits & (1u << i)) &&
            (mp.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)) {
            index = (int)i;
            break;
        }
    }
    if (index < 0) { P("PROBE-RENDER %s no-host-visible", tag); vkDestroyBuffer(g_device, buffer, NULL); return; }
    VkMemoryAllocateInfo ai = {0};
    ai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = (uint32_t)index;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    r = vkAllocateMemory(g_device, &ai, NULL, &memory);
    if (r != VK_SUCCESS) { P("PROBE-RENDER %s mem-alloc=%d", tag, r); vkDestroyBuffer(g_device, buffer, NULL); return; }
    vkBindBufferMemory(g_device, buffer, memory, 0);

    VkCommandPoolCreateInfo pci = {0};
    pci.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = g_queue_family;
    VkCommandPool pool = VK_NULL_HANDLE;
    vkCreateCommandPool(g_device, &pci, NULL, &pool);
    VkCommandBufferAllocateInfo cbi = {0};
    cbi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    cbi.commandPool = pool;
    cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbi.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    vkAllocateCommandBuffers(g_device, &cbi, &cmd);

    VkCommandBufferBeginInfo bi = {0};
    bi.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmd, &bi);

    VkImageSubresourceRange range = {0};
    range.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    range.levelCount = 1;
    range.layerCount = 1;

    VkImageMemoryBarrier to_general = {0};
    to_general.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    to_general.srcAccessMask = 0;
    to_general.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    to_general.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    to_general.newLayout = VK_IMAGE_LAYOUT_GENERAL;
    to_general.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    to_general.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    to_general.image = image;
    to_general.subresourceRange = range;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                         0, 0, NULL, 0, NULL, 1, &to_general);

    VkClearColorValue clear = {{1.0f, 0.0f, 1.0f, 1.0f}};  // magenta
    vkCmdClearColorImage(cmd, image, VK_IMAGE_LAYOUT_GENERAL, &clear, 1, &range);

    VkImageMemoryBarrier to_src = to_general;
    to_src.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    to_src.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    to_src.oldLayout = VK_IMAGE_LAYOUT_GENERAL;
    to_src.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, NULL, 0, NULL, 1, &to_src);

    VkBufferImageCopy region = {0};
    region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    region.imageSubresource.layerCount = 1;
    region.imageExtent.width = WIDTH;
    region.imageExtent.height = HEIGHT;
    region.imageExtent.depth = 1;
    vkCmdCopyImageToBuffer(cmd, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, buffer, 1, &region);
    vkEndCommandBuffer(cmd);

    VkSubmitInfo si = {0};
    si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cmd;
    r = vkQueueSubmit(g_queue, 1, &si, VK_NULL_HANDLE);
    if (r != VK_SUCCESS) { P("PROBE-RENDER %s submit=%d", tag, r); return; }
    r = vkQueueWaitIdle(g_queue);
    if (r != VK_SUCCESS) { P("PROBE-RENDER %s wait=%d", tag, r); return; }

    void *mapped = NULL;
    r = vkMapMemory(g_device, memory, 0, VK_WHOLE_SIZE, 0, &mapped);
    if (r != VK_SUCCESS) { P("PROBE-RENDER %s map=%d", tag, r); return; }
    unsigned char *px = (unsigned char *)mapped;
    P("PROBE-PIXELS %s BGRa=%u,%u,%u,%u (expect 255,0,255,255)", tag,
      px[0], px[1], px[2], px[3]);
    vkUnmapMemory(g_device, memory);

    vkDestroyCommandPool(g_device, pool, NULL);
    vkDestroyBuffer(g_device, buffer, NULL);
    vkFreeMemory(g_device, memory, NULL);
}

int main(void) {
    char ctx[128] = {0};
    int n = (int)readlink("/proc/self/attr/current", ctx, sizeof(ctx) - 1);
    if (n < 0) n = 0;
    ctx[n] = 0;
    P("PROBE-START uid=%d selinux=%s", (int)geteuid(), ctx);

    VkApplicationInfo app = {0};
    app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app.pApplicationName = "dmaheap-probe";
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ici = {0};
    ici.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    ici.pApplicationInfo = &app;
    VkResult r = vkCreateInstance(&ici, NULL, &g_instance);
    P("PROBE-INSTANCE result=%d", r);
    if (r != VK_SUCCESS) return 0;

    uint32_t count = 0;
    vkEnumeratePhysicalDevices(g_instance, &count, NULL);
    VkPhysicalDevice *devs = calloc(count ? count : 1, sizeof(*devs));
    vkEnumeratePhysicalDevices(g_instance, &count, devs);
    P("PROBE-DEVICES count=%u", count);
    if (count == 0) return 0;
    g_phys = devs[0];
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(g_phys, &props);
    P("PROBE-DEVICE name=%s api=%u.%u.%u driver=0x%08x vendor=0x%04x", props.deviceName,
      VK_API_VERSION_MAJOR(props.apiVersion), VK_API_VERSION_MINOR(props.apiVersion),
      VK_API_VERSION_PATCH(props.apiVersion), props.driverVersion, props.vendorID);

    int has_dmabuf = has_ext(VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME);
    int has_drmmod = has_ext("VK_EXT_image_drm_format_modifier");
    int has_ahb = has_ext("VK_ANDROID_external_memory_android_hardware_buffer");
    int has_host = has_ext("VK_EXT_external_memory_host");
    P("PROBE-EXT dma_buf=%d drm_modifier=%d ahb=%d external_host=%d",
      has_dmabuf, has_drmmod, has_ahb, has_host);

    if (has_host) {
        VkPhysicalDeviceExternalMemoryHostPropertiesEXT hp = {0};
        hp.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT;
        VkPhysicalDeviceProperties2 p2 = {0};
        p2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
        p2.pNext = &hp;
        vkGetPhysicalDeviceProperties2(g_phys, &p2);
        P("PROBE-HOST-PTR minAlign=%llu", (unsigned long long)hp.minImportedHostPointerAlignment);
    }

    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(g_phys, &qn, NULL);
    VkQueueFamilyProperties *qf = calloc(qn, sizeof(*qf));
    vkGetPhysicalDeviceQueueFamilyProperties(g_phys, &qn, qf);
    g_queue_family = 0;
    for (uint32_t i = 0; i < qn; i++) {
        if (qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) { g_queue_family = i; break; }
    }
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci = {0};
    qci.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    qci.queueFamilyIndex = g_queue_family;
    qci.queueCount = 1;
    qci.pQueuePriorities = &prio;

    const char *exts[4];
    uint32_t ext_count = 0;
    if (has_dmabuf) exts[ext_count++] = VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME;
    if (has_drmmod) exts[ext_count++] = "VK_EXT_image_drm_format_modifier";
    VkPhysicalDeviceFeatures features = {0};
    VkDeviceCreateInfo dci = {0};
    dci.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = ext_count;
    dci.ppEnabledExtensionNames = exts;
    dci.pEnabledFeatures = &features;
    r = vkCreateDevice(g_phys, &dci, NULL, &g_device);
    P("PROBE-DEVICE-CREATE result=%d", r);
    if (r != VK_SUCCESS) return 0;
    vkGetDeviceQueue(g_device, g_queue_family, 0, &g_queue);

    // Capability query for the exact tuple zink uses.
    VkPhysicalDeviceImageDrmFormatModifierInfoEXT mod = {0};
    mod.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT;
    mod.drmFormatModifier = 0;
    mod.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkPhysicalDeviceExternalImageFormatInfo ext_fmt = {0};
    ext_fmt.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO;
    ext_fmt.pNext = &mod;
    ext_fmt.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    VkPhysicalDeviceImageFormatInfo2 fmt = {0};
    fmt.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2;
    fmt.pNext = &ext_fmt;
    fmt.format = VK_FORMAT_B8G8R8A8_UNORM;
    fmt.type = VK_IMAGE_TYPE_2D;
    fmt.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
    fmt.usage = USAGE;
    VkExternalImageFormatProperties ext_props = {0};
    ext_props.sType = VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES;
    VkImageFormatProperties2 ifp = {0};
    ifp.sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2;
    ifp.pNext = &ext_props;
    r = vkGetPhysicalDeviceImageFormatProperties2(g_phys, &fmt, &ifp);
    P("PROBE-FORMAT-QUERY result=%d extFeatures=0x%x exportFromImported=0x%x compatible=0x%x",
      r, ext_props.externalMemoryProperties.externalMemoryFeatures,
      ext_props.externalMemoryProperties.exportFromImportedHandleTypes,
      ext_props.externalMemoryProperties.compatibleHandleTypes);

    uint64_t need = align_up(IMAGE_SIZE, 4096);
    const char *heap_paths[] = {"/dev/dma_heap/system", "/dev/dma_heap/system-uncached"};
    for (unsigned i = 0; i < 2; i++) {
        int fd = make_heap_fd(heap_paths[i], need);
        if (fd < 0) continue;
        VkImage image = VK_NULL_HANDLE;
        const char *tag = i == 0 ? "heap-system" : "heap-uncached";
        if (create_image(&image, tag) == VK_SUCCESS) {
            if (import_image(fd, image, tag) == VK_SUCCESS) render_check(image, tag);
            vkDestroyImage(g_device, image, NULL);
        }
        close(fd);
    }

    int udma = make_udmabuf_fd(need);
    if (udma >= 0) {
        VkImage image = VK_NULL_HANDLE;
        if (create_image(&image, "udmabuf") == VK_SUCCESS) {
            if (import_image(udma, image, "udmabuf") == VK_SUCCESS) render_check(image, "udmabuf");
            vkDestroyImage(g_device, image, NULL);
        }
        close(udma);
    }

    vkDeviceWaitIdle(g_device);
    P("PROBE-DONE");
    return 0;
}
