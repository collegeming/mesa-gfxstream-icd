// Mali external-memory import probe for Android hosts (v2).
//
// Question 1: does this Mali driver import a /dev/dma_heap dma-buf as a
// VkImage (and as a VkBuffer)? Question 2: which exact VkImageCreateInfo shape
// does it accept for DMA_BUF + explicit LINEAR, given that the gfxstream host
// process succeeded with the same tuple from the guest while a naive standalone
// create fails with VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT?
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

#ifndef VK_USE_PLATFORM_ANDROID_KHR
#define VK_USE_PLATFORM_ANDROID_KHR 1
#endif
#include <android/hardware_buffer.h>
#include <vulkan/vulkan.h>

struct dma_heap_allocation_data {
    uint64_t len;
    uint32_t fd;
    uint32_t fd_flags;
    uint64_t heap_flags;
};
#define DMA_HEAP_IOC_MAGIC 'H'
#define DMA_HEAP_IOCTL_ALLOC _IOWR(DMA_HEAP_IOC_MAGIC, 0, struct dma_heap_allocation_data)

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
    if (heap < 0) heap = open(path, O_RDWR | O_CLOEXEC);
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

struct create_variant {
    const char *tag;
    VkImageCreateFlags flags;
    int view_formats;
    int explicit_mod;
    uint64_t plane_size;
    VkImageUsageFlags usage;
};

static VkResult create_image(const struct create_variant *v, VkImage *image) {
    VkImageFormatListCreateInfo view_list = {0};
    view_list.sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO;
    static const VkFormat formats[2] = {VK_FORMAT_B8G8R8A8_UNORM,
                                        VK_FORMAT_B8G8R8A8_SRGB};
    if (v->view_formats) {
        view_list.viewFormatCount = 2;
        view_list.pViewFormats = formats;
    }

    VkSubresourceLayout plane = {0};
    plane.offset = 0;
    plane.rowPitch = STRIDE;
    plane.size = v->plane_size;

    VkImageDrmFormatModifierExplicitCreateInfoEXT explicit_mod = {0};
    explicit_mod.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT;
    explicit_mod.drmFormatModifier = 0;  // DRM_FORMAT_MOD_LINEAR
    explicit_mod.drmFormatModifierPlaneCount = 1;
    explicit_mod.pPlaneLayouts = &plane;

    uint64_t modifier = 0;
    VkImageDrmFormatModifierListCreateInfoEXT list_mod = {0};
    list_mod.sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT;
    list_mod.drmFormatModifierCount = 1;
    list_mod.pDrmFormatModifiers = &modifier;

    VkExternalMemoryImageCreateInfo external = {0};
    external.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO;
    external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;

    void *tail = v->view_formats ? (void *)&view_list : NULL;
    external.pNext = tail;
    if (v->explicit_mod) {
        explicit_mod.pNext = &external;
        tail = &explicit_mod;
    } else {
        list_mod.pNext = &external;
        tail = &list_mod;
    }

    VkImageCreateInfo ci = {0};
    ci.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    ci.pNext = tail;
    ci.flags = v->flags;
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = VK_FORMAT_B8G8R8A8_UNORM;
    ci.extent.width = WIDTH;
    ci.extent.height = HEIGHT;
    ci.extent.depth = 1;
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
    ci.usage = v->usage;
    ci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VkResult r = vkCreateImage(g_device, &ci, NULL, image);
    P("PROBE-IMAGE-CREATE %s result=%d image=%p", v->tag, r, (void *)*image);
    return r;
}

static void render_check(VkImage image, const char *tag);

static VkResult import_image_type(int fd, VkImage image, const char *tag, int index) {
    VkMemoryRequirements req = {0};
    vkGetImageMemoryRequirements(g_device, image, &req);

    VkMemoryDedicatedAllocateInfo dedicated = {0};
    dedicated.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO;
    dedicated.image = image;

    VkImportMemoryFdInfoKHR import = {0};
    import.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR;
    import.pNext = &dedicated;
    import.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    import.fd = dup(fd);

    VkMemoryAllocateInfo ai = {0};
    ai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    ai.pNext = &import;
    ai.allocationSize = align_up(req.size, 4096);
    ai.memoryTypeIndex = (uint32_t)index;

    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkResult r = vkAllocateMemory(g_device, &ai, NULL, &memory);
    P("PROBE-IMPORT %s type=%d result=%d memory=%p", tag, index, r, (void *)memory);
    if (r != VK_SUCCESS) return r;

    r = vkBindImageMemory(g_device, image, memory, 0);
    P("PROBE-BIND %s type=%d result=%d", tag, index, r);
    return r;
}

static void import_image(int fd, VkImage image, const char *tag) {
    VkMemoryRequirements req = {0};
    vkGetImageMemoryRequirements(g_device, image, &req);
    P("PROBE-IMG-REQ %s size=%llu align=%llu bits=0x%x fd=%d", tag,
      (unsigned long long)req.size, (unsigned long long)req.alignment, req.memoryTypeBits, fd);
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(g_phys, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++) {
        if ((req.memoryTypeBits & (1u << i)) == 0) continue;
        VkResult r = import_image_type(fd, image, tag, (int)i);
        if (r == VK_SUCCESS) {
            render_check(image, tag);
            return;
        }
    }
}

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
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                         VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                         0, 0, NULL, 0, NULL, 1, &to_general);

    VkClearColorValue clear = {{1.0f, 0.0f, 1.0f, 1.0f}};  // magenta
    vkCmdClearColorImage(cmd, image, VK_IMAGE_LAYOUT_GENERAL, &clear, 1, &range);

    VkImageMemoryBarrier to_src = to_general;
    to_src.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    to_src.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    to_src.oldLayout = VK_IMAGE_LAYOUT_GENERAL;
    to_src.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT,
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

static void buffer_import_test(int fd, const char *tag) {
    VkExternalMemoryBufferCreateInfo ext = {0};
    ext.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO;
    ext.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    VkBufferCreateInfo bci = {0};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.pNext = &ext;
    bci.size = IMAGE_SIZE;
    bci.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkBuffer buffer = VK_NULL_HANDLE;
    VkResult r = vkCreateBuffer(g_device, &bci, NULL, &buffer);
    P("PROBE-BUF-CREATE %s result=%d", tag, r);
    if (r != VK_SUCCESS) return;

    VkMemoryRequirements req = {0};
    vkGetBufferMemoryRequirements(g_device, buffer, &req);
    P("PROBE-BUF-REQ %s size=%llu bits=0x%x", tag, (unsigned long long)req.size, req.memoryTypeBits);
    for (uint32_t i = 0; i < 32; i++) {
        if ((req.memoryTypeBits & (1u << i)) == 0) continue;
        VkMemoryDedicatedAllocateInfo dedicated = {0};
        dedicated.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO;
        dedicated.buffer = buffer;
        VkImportMemoryFdInfoKHR import = {0};
        import.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR;
        import.pNext = &dedicated;
        import.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        import.fd = dup(fd);
        VkMemoryAllocateInfo ai = {0};
        ai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        ai.pNext = &import;
        ai.allocationSize = align_up(req.size, 4096);
        ai.memoryTypeIndex = i;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        r = vkAllocateMemory(g_device, &ai, NULL, &memory);
        P("PROBE-BUF-IMPORT %s type=%u result=%d", tag, i, r);
        if (r == VK_SUCCESS) {
            r = vkBindBufferMemory(g_device, buffer, memory, 0);
            P("PROBE-BUF-BIND %s type=%u result=%d", tag, i, r);
            break;
        }
    }
    vkDestroyBuffer(g_device, buffer, NULL);
}


// Can Mali use VK_EXT_external_memory_host for *images*? If yes, the GpuPool's
// host VA (or any host-mapped guest RAM) can back a Mali image with no dma-buf
// exporter at all. Query first, then import an ordinary page-aligned host buffer.
static void host_pointer_test(void) {
    VkPhysicalDeviceImageDrmFormatModifierInfoEXT mod = {0};
    mod.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT;
    mod.drmFormatModifier = 0;
    mod.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkPhysicalDeviceExternalImageFormatInfo ext_fmt = {0};
    ext_fmt.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO;
    ext_fmt.pNext = &mod;
    ext_fmt.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
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
    VkResult r = vkGetPhysicalDeviceImageFormatProperties2(g_phys, &fmt, &ifp);
    P("PROBE-HOST-QUERY result=%d extFeatures=0x%x compatible=0x%x", r,
      ext_props.externalMemoryProperties.externalMemoryFeatures,
      ext_props.externalMemoryProperties.compatibleHandleTypes);
    if (r != VK_SUCCESS) return;

    struct create_variant v = {"hostptr", VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT, 1, 1, 0, USAGE};
    VkImage image = VK_NULL_HANDLE;
    if (create_image(&v, &image) != VK_SUCCESS) return;

    void *host = aligned_alloc(4096, (size_t)IMAGE_SIZE);
    if (host) memset(host, 0x5a, (size_t)IMAGE_SIZE);
    P("PROBE-HOST-ALLOC ptr=%p aligned=%d", host, host && ((uintptr_t)host % 4096) == 0);

    VkMemoryRequirements req = {0};
    vkGetImageMemoryRequirements(g_device, image, &req);
    P("PROBE-HOST-REQ size=%llu bits=0x%x", (unsigned long long)req.size, req.memoryTypeBits);
    for (uint32_t i = 0; host && i < 32; i++) {
        if ((req.memoryTypeBits & (1u << i)) == 0) continue;
        VkMemoryDedicatedAllocateInfo dedicated = {0};
        dedicated.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO;
        dedicated.image = image;
        VkImportMemoryHostPointerInfoEXT imp = {0};
        imp.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT;
        imp.pNext = &dedicated;
        imp.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
        imp.pHostPointer = host;
        VkMemoryAllocateInfo ai = {0};
        ai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        ai.pNext = &imp;
        ai.allocationSize = align_up(req.size, 4096);
        ai.memoryTypeIndex = i;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        r = vkAllocateMemory(g_device, &ai, NULL, &memory);
        P("PROBE-HOST-IMAGE-IMPORT type=%u result=%d", i, r);
        if (r == VK_SUCCESS) {
            r = vkBindImageMemory(g_device, image, memory, 0);
            P("PROBE-HOST-IMAGE-BIND type=%u result=%d", i, r);
            if (r == VK_SUCCESS) render_check(image, "hostptr");
            break;
        }
    }
    vkDestroyImage(g_device, image, NULL);
    free(host);
}


// Can the host allocate a buffer-shaped AHB itself (BLOB + GPU_DATA_BUFFER) and
// import it into Mali for a VkBuffer? This is the non-export alternative for the
// guest's classic buffer memory: Mali cannot export to AHB, but it can import.
static void ahb_buffer_test(void) {
    if (!has_ext("VK_ANDROID_external_memory_android_hardware_buffer")) {
        P("PROBE-AHB-BUF skipped (no AHB extension)");
        return;
    }
    AHardwareBuffer_Desc desc = {0};
    desc.width = 4096;
    desc.height = 1;
    desc.layers = 1;
    desc.format = AHARDWAREBUFFER_FORMAT_BLOB;
    desc.usage = AHARDWAREBUFFER_USAGE_GPU_DATA_BUFFER | AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN |
                 AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN;
    AHardwareBuffer* ahb = NULL;
    int ahbRes = AHardwareBuffer_allocate(&desc, &ahb);
    P("PROBE-AHB-BUF-ALLOC result=%d ahb=%p", ahbRes, (void*)ahb);
    if (ahbRes != 0 || !ahb) return;

    VkAndroidHardwareBufferPropertiesANDROID props = {0};
    props.sType = VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_PROPERTIES_ANDROID;
    VkResult r = vkGetAndroidHardwareBufferPropertiesANDROID(g_device, ahb, &props);
    P("PROBE-AHB-BUF-PROPS result=%d bits=0x%x allocSize=%llu", r, props.memoryTypeBits,
      (unsigned long long)props.allocationSize);

    VkExternalMemoryBufferCreateInfo ext = {0};
    ext.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO;
    ext.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID;
    static const struct { const char* tag; VkBufferUsageFlags usage; } kUsages[] = {
        {"transfer", VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT},
        {"storage", VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT |
                        VK_BUFFER_USAGE_TRANSFER_DST_BIT},
        {"uniform", VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT | VK_BUFFER_USAGE_STORAGE_BUFFER_BIT |
                        VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT},
    };
    for (unsigned u = 0; u < sizeof(kUsages) / sizeof(kUsages[0]); u++) {
    VkBufferCreateInfo bci = {0};
    bci.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bci.pNext = &ext;
    bci.size = 4096;
    bci.usage = kUsages[u].usage;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VkBuffer buffer = VK_NULL_HANDLE;
    r = vkCreateBuffer(g_device, &bci, NULL, &buffer);
    P("PROBE-AHB-BUF-CREATE %s result=%d", kUsages[u].tag, r);
    if (r != VK_SUCCESS) continue;

    VkMemoryRequirements req = {0};
    vkGetBufferMemoryRequirements(g_device, buffer, &req);
    P("PROBE-AHB-BUF-REQ size=%llu bits=0x%x", (unsigned long long)req.size, req.memoryTypeBits);
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(g_phys, &mp);
    int chosen = -1;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++) {
        if ((req.memoryTypeBits & props.memoryTypeBits & (1u << i)) != 0) {
            if (chosen < 0) chosen = (int)i;
            P("PROBE-AHB-BUF-MEMTYPE index=%u flags=0x%x", i, mp.memoryTypes[i].propertyFlags);
        }
    }
    P("PROBE-AHB-BUF-CHOSEN index=%d", chosen);

    if (chosen >= 0) {
        VkImportAndroidHardwareBufferInfoANDROID imp = {0};
        imp.sType = VK_STRUCTURE_TYPE_IMPORT_ANDROID_HARDWARE_BUFFER_INFO_ANDROID;
        imp.buffer = ahb;
        VkMemoryAllocateInfo ai = {0};
        ai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        ai.pNext = &imp;
        ai.allocationSize = props.allocationSize;
        ai.memoryTypeIndex = (uint32_t)chosen;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        r = vkAllocateMemory(g_device, &ai, NULL, &memory);
        P("PROBE-AHB-BUF-IMPORT result=%d", r);
        if (r == VK_SUCCESS) {
            r = vkBindBufferMemory(g_device, buffer, memory, 0);
            P("PROBE-AHB-BUF-BIND result=%d", r);
            void* mapped = NULL;
            r = vkMapMemory(g_device, memory, 0, 4096, 0, &mapped);
            P("PROBE-AHB-BUF-MAP result=%d ptr=%p", r, mapped);
            if (r == VK_SUCCESS) {
                ((unsigned char*)mapped)[0] = 0xAB;
                ((unsigned char*)mapped)[1] = 0xCD;
                P("PROBE-AHB-BUF-PIXELS %u,%u (expect 171,205)", ((unsigned char*)mapped)[0],
                  ((unsigned char*)mapped)[1]);
                vkUnmapMemory(g_device, memory);
            }
        }
    }
    vkDestroyBuffer(g_device, buffer, NULL);
    }
    AHardwareBuffer_release(ahb);
}

int main(void) {
    char ctx[128] = {0};
    int n = (int)readlink("/proc/self/attr/current", ctx, sizeof(ctx) - 1);
    if (n < 0) n = 0;
    ctx[n] = 0;
    P("PROBE-START uid=%d selinux=%s", (int)geteuid(), ctx);

    VkApplicationInfo app = {0};
    app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app.pApplicationName = "dmaheap-probe2";
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

    const char *ext_names[] = {"VK_EXT_external_memory_dma_buf", "VK_EXT_image_drm_format_modifier",
                               "VK_ANDROID_external_memory_android_hardware_buffer",
                               "VK_EXT_external_memory_host"};
    int have[4];
    for (int i = 0; i < 4; i++) have[i] = has_ext(ext_names[i]);
    P("PROBE-EXT dma_buf=%d drm_modifier=%d ahb=%d external_host=%d",
      have[0], have[1], have[2], have[3]);

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

    // Enable everything the real gfxstream host device enables for this feature set.
    const char *enabled[4];
    uint32_t enabled_count = 0;
    for (int i = 0; i < 4; i++)
        if (have[i]) enabled[enabled_count++] = ext_names[i];
    VkPhysicalDeviceFeatures features = {0};
    VkDeviceCreateInfo dci = {0};
    dci.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = enabled_count;
    dci.ppEnabledExtensionNames = enabled;
    dci.pEnabledFeatures = &features;
    r = vkCreateDevice(g_phys, &dci, NULL, &g_device);
    P("PROBE-DEVICE-CREATE result=%d exts=%u", r, enabled_count);
    if (r != VK_SUCCESS) return 0;
    vkGetDeviceQueue(g_device, g_queue_family, 0, &g_queue);

    // Advertised DRM modifier list for this format/usage.
    VkDrmFormatModifierPropertiesListEXT mod_list = {0};
    mod_list.sType = VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT;
    VkFormatProperties2 fmt_props = {0};
    fmt_props.sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2;
    fmt_props.pNext = &mod_list;
    vkGetPhysicalDeviceFormatProperties2(g_phys, VK_FORMAT_B8G8R8A8_UNORM, &fmt_props);
    P("PROBE-MODLIST count=%u", mod_list.drmFormatModifierCount);
    if (mod_list.drmFormatModifierCount) {
        VkDrmFormatModifierPropertiesEXT *mods = calloc(mod_list.drmFormatModifierCount, sizeof(*mods));
        mod_list.pDrmFormatModifierProperties = mods;
        vkGetPhysicalDeviceFormatProperties2(g_phys, VK_FORMAT_B8G8R8A8_UNORM, &fmt_props);
        for (uint32_t i = 0; i < mod_list.drmFormatModifierCount && i < 8; i++) {
            P("PROBE-MODLIST[%u] modifier=0x%llx planes=%u tiling=0x%x", i,
              (unsigned long long)mods[i].drmFormatModifier, mods[i].drmFormatModifierPlaneCount,
              mods[i].drmFormatModifierTilingFeatures);
        }
        free(mods);
    }

    // Exact tuple query zink uses.
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
    const char *heaps[] = {"/dev/dma_heap/system", "/dev/dma_heap/system-uncached",
                           "/dev/dma_heap/linux,cma", "/dev/dma_heap/xring_heap_drm",
                           "/dev/dma_heap/xring_cpa"};
    struct create_variant img_variants[] = {
        {"mutable-sz0", VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT, 1, 1, 0, USAGE},
        {"flat-list",   0,                                  0, 0, 0, USAGE},
    };
    for (unsigned h = 0; h < sizeof(heaps) / sizeof(heaps[0]); h++) {
        int fd = make_heap_fd(heaps[h], need);
        if (fd < 0) continue;
        char tag[96];
        const char *base = strrchr(heaps[h], '/');
        base = base ? base + 1 : heaps[h];
        snprintf(tag, sizeof(tag), "buf:%s", base);
        buffer_import_test(fd, tag);
        for (unsigned v = 0; v < sizeof(img_variants) / sizeof(img_variants[0]); v++) {
            VkImage image = VK_NULL_HANDLE;
            if (create_image(&img_variants[v], &image) != VK_SUCCESS) continue;
            snprintf(tag, sizeof(tag), "img:%s:%s", base, img_variants[v].tag);
            import_image(fd, image, tag);
            vkDestroyImage(g_device, image, NULL);
        }
        close(fd);
    }

    host_pointer_test();
    ahb_buffer_test();

    P("PROBE-DONE");
    return 0;
}
