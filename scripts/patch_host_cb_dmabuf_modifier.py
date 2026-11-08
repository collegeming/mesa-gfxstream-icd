#!/usr/bin/env python3
"""D4b host patch: give the dma-buf ColorBuffer image an explicit DRM LINEAR
modifier layout instead of a plain OPTIMAL/LINEAR image with only an external
handle type.

Applied after patch_host_p2d_image_dmabuf.py (D1) on top of the pinned DroidVM
gfxstream tree.  All changes are host-only:

  1. createVkColorBufferLocked(): when p2dExternalMode == 1 (dmabuf), set the
     ColorBuffer image tiling to VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT and
     chain VkImageDrmFormatModifierListCreateInfoEXT{1, {DRM_FORMAT_MOD_LINEAR}}
     behind VkExternalMemoryImageCreateInfo{DMA_BUF}.  Mali rejects a dma-buf
     image without a modifier declaration with
     VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT.
  2. Both D1 fallback arms (IMAGE-CREATE-FALLBACK and IMAGE-SIZE-ZERO) reset
     imageCi->tiling to VK_IMAGE_TILING_OPTIMAL before recreating the image;
     otherwise the fallback is a modifier-tiled image with no modifier list.
  3. The CIB1 createClassicImageLocked() bridge re-attaches the same LINEAR
     modifier list to the alias image: imageCreateInfoShallow is an orphan copy
     with pNext == nullptr, so the modifier does not survive the shallow copy.

D1's SKIP-AHB allocation, VkExportMemoryAllocateInfo{DMA_BUF},
vkGetMemoryFdKHR export, MEM_DMABUF handleInfo and zero-size import whitelist
are left untouched.  Every anchor must occur exactly once; all post-checks must
pass; a mismatch aborts the build and leaves every file untouched.
"""

import sys
from pathlib import Path

OPS = Path("host/vulkan/vk_common_operations.cpp")
OPS_H = Path("host/vulkan/vk_common_operations.h")
DEC = Path("host/vulkan/vk_decoder_global_state.cpp")

# -------------------------------------------------- modifier list declaration
EXT_DECL_OLD = b"""    VkExternalMemoryImageCreateInfo extImageCi = {
        VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO
    };
    const char* p2dModeName = infoPtr->p2dExternalMode == 1
"""

EXT_DECL_NEW = b"""    VkExternalMemoryImageCreateInfo extImageCi = {
        VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO
    };
    // D4B: DRM_FORMAT_MOD_LINEAR == 0.  Kept local to this function; the struct
    // points at it only for the synchronous vkCreateImage call below.
    static const uint64_t p2dLinearModifier = 0;
    VkImageDrmFormatModifierListCreateInfoEXT p2dModifierList = {
        VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT,
        nullptr,
        1,
        &p2dLinearModifier,
    };
    const char* p2dModeName = infoPtr->p2dExternalMode == 1
"""

# ------------------------------------------------------------- tiling override
TILING_OLD = b"""    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
                               ? VK_IMAGE_TILING_LINEAR
                               : VK_IMAGE_TILING_OPTIMAL;
    std::unique_ptr<VkImageCreateInfo> imageCi = generateColorBufferVkImageCreateInfoLocked(
"""

TILING_NEW = b"""    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
                               ? VK_IMAGE_TILING_LINEAR
                               : VK_IMAGE_TILING_OPTIMAL;
    if (infoPtr->p2dExternalMode == 1) {
        // D4B: a dma-buf image must describe its DRM layout.  Mali returns
        // VK_ERROR_INVALID_DRM_FORMAT_MODIFIER_PLANE_LAYOUT_EXT for the D1 shape
        // (OPTIMAL + DMA_BUF and no modifier structure).
        tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
    }
    std::unique_ptr<VkImageCreateInfo> imageCi = generateColorBufferVkImageCreateInfoLocked(
"""

# ------------------------------------------------------- modifier pNext + log
EXT_CHAIN_OLD = b"""        imageCi->pNext = &extImageCi;
    }
    GFXSTREAM_WARNING(
        "GFXDIAG: D1 P2D-IMAGE-EXT mode=%s handleTypes=0x%x supportsDmaBuf=%d cb=%u "
        "externalMode=%d",
        p2dModeName, (unsigned)extImageCi.handleTypes, (int)mDeviceInfo.supportsDmaBuf,
        colorBufferHandle, (int)getExternalMemoryMode());
"""

EXT_CHAIN_NEW = b"""        if (infoPtr->p2dExternalMode == 1) {
            // D4B: chain image -> external(DMA_BUF) -> modifier list(LINEAR).
            extImageCi.pNext = &p2dModifierList;
        }
        imageCi->pNext = &extImageCi;
    }
    GFXSTREAM_WARNING(
        "GFXDIAG: D1 P2D-IMAGE-EXT mode=%s handleTypes=0x%x supportsDmaBuf=%d cb=%u "
        "externalMode=%d",
        p2dModeName, (unsigned)extImageCi.handleTypes, (int)mDeviceInfo.supportsDmaBuf,
        colorBufferHandle, (int)getExternalMemoryMode());
    GFXSTREAM_WARNING(
        "GFXDIAG: D4B P2D-IMAGE-MOD mode=%s tiling=%d mod=DRM_FORMAT_MOD_LINEAR(0x%llx) "
        "handleTypes=0x%x usage=0x%x flags=0x%x cb=%u",
        p2dModeName, (int)imageCi->tiling, (unsigned long long)p2dLinearModifier,
        (unsigned)extImageCi.handleTypes, imageCi->usage, imageCi->flags, colorBufferHandle);
"""

# --------------------------------------------------- create fallback tiling reset
CREATE_FALLBACK_OLD = b"""            (int)createRes, colorBufferHandle, (int)vkFormat, width, height,
            (unsigned)extImageCi.handleTypes);
        extImageCi.handleTypes = 0;
        imageCi->pNext = nullptr;
"""

CREATE_FALLBACK_NEW = b"""            (int)createRes, colorBufferHandle, (int)vkFormat, width, height,
            (unsigned)extImageCi.handleTypes);
        imageCi->tiling = VK_IMAGE_TILING_OPTIMAL;
        extImageCi.handleTypes = 0;
        extImageCi.pNext = nullptr;
        imageCi->pNext = nullptr;
"""

# ----------------------------------------------------- size fallback tiling reset
SIZE_FALLBACK_OLD = b"""            (unsigned)infoPtr->imageMemReqs.memoryTypeBits);
        vk->vkDestroyImage(mDevice, infoPtr->image, nullptr);
        infoPtr->image = VK_NULL_HANDLE;
        extImageCi.handleTypes = 0;
        imageCi->pNext = nullptr;
"""

SIZE_FALLBACK_NEW = b"""            (unsigned)infoPtr->imageMemReqs.memoryTypeBits);
        vk->vkDestroyImage(mDevice, infoPtr->image, nullptr);
        infoPtr->image = VK_NULL_HANDLE;
        imageCi->tiling = VK_IMAGE_TILING_OPTIMAL;
        extImageCi.handleTypes = 0;
        extImageCi.pNext = nullptr;
        imageCi->pNext = nullptr;
"""

# ------------------------------------------------------- image requirement log
BITS_OLD = b"""        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL
"""

BITS_NEW = b"""        vk->vkGetImageMemoryRequirements(mDevice, infoPtr->image, &infoPtr->imageMemReqs);
    }

    if (infoPtr->p2dExternalMode == 1) {
        GFXSTREAM_WARNING(
            "GFXDIAG: D4B P2D-IMAGE-BITS cb=%u fmt=%d %ux%u size=%llu bits=0x%x tiling=%d",
            colorBufferHandle, (int)vkFormat, width, height,
            (unsigned long long)infoPtr->imageMemReqs.size,
            (unsigned)infoPtr->imageMemReqs.memoryTypeBits,
            (int)infoPtr->imageCreateInfoShallow.tiling);
    }

    // Currently we only care about two memory properties: DEVICE_LOCAL
"""

# ------------------------------------------------------------- export trace
EXPORT_OK_OLD = b"""                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=1 fd=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x%x",
                p2dExportFd, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size, (unsigned)STREAM_HANDLE_TYPE_MEM_DMABUF);
"""

EXPORT_OK_NEW = b"""                "GFXDIAG: D1 P2D-DMABUF-EXPORT ok=1 fd=%d cb=%u memindex=%u size=%llu "
                "handleInfo=0x%x",
                p2dExportFd, (*colorBufferInfo)->handle, info->typeIndex,
                (unsigned long long)info->size, (unsigned)STREAM_HANDLE_TYPE_MEM_DMABUF);
            GFXSTREAM_WARNING(
                "GFXDIAG: D4B P2D-DMABUF-EXPORT tiling=%d mod=DRM_FORMAT_MOD_LINEAR(0x0) "
                "exportTypes=0x%x ok=1 fd=%d cb=%u",
                (int)(*colorBufferInfo)->imageCreateInfoShallow.tiling,
                (unsigned)VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, p2dExportFd,
                (*colorBufferInfo)->handle);
"""

# ------------------------------------------------------------ CIB1 alias image
CIB_IMAGE_OLD = b"""        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? &localList : nullptr,
            p2dBridgeDmaBuf ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                            : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        ci.pNext = &external;
"""

CIB_IMAGE_NEW = b"""        // D4B: imageCreateInfoShallow is an orphan copy (pNext == nullptr), so the
        // ColorBuffer's DRM modifier list does not survive the copy.  Re-attach the
        // same LINEAR modifier list to the alias image.
        const bool p2dBridgeModifierTiling =
            ci.tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
        static const uint64_t p2dBridgeLinearModifier = 0;
        VkImageDrmFormatModifierListCreateInfoEXT p2dBridgeModifierList{
            VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT,
            nullptr,
            1,
            &p2dBridgeLinearModifier,
        };
        if (list && p2dBridgeModifierTiling) {
            // Keep the view-format list and the modifier list on one pNext chain.
            localList.pNext = &p2dBridgeModifierList;
        }
        VkExternalMemoryImageCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
            list ? static_cast<const void*>(&localList)
                 : (p2dBridgeModifierTiling
                        ? static_cast<const void*>(&p2dBridgeModifierList)
                        : nullptr),
            p2dBridgeDmaBuf ? VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT
                            : VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
        ci.pNext = &external;
        if (p2dBridgeModifierTiling) {
            GFXSTREAM_WARNING(
                "GFXDIAG: D4B CIB1-IMAGE-MOD tiling=%d mod=DRM_FORMAT_MOD_LINEAR(0x%llx) "
                "handleTypes=0x%x list=%d cb=%u",
                (int)ci.tiling, (unsigned long long)p2dBridgeLinearModifier,
                (unsigned)external.handleTypes, list ? 1 : 0, resource);
        }
"""

MARKERS = [
    b"GFXDIAG: D4B P2D-IMAGE-MOD",
    b"GFXDIAG: D4B P2D-IMAGE-BITS",
    b"GFXDIAG: D4B P2D-DMABUF-EXPORT",
    b"GFXDIAG: D4B CIB1-IMAGE-MOD",
]


def fail(message):
    print(f"patch_host_cb_dmabuf_modifier: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def replace_once(data, old, new, label):
    count = data.count(old)
    if count != 1:
        fail(f"anchor '{label}' count is {count}, expected exactly 1")
    return data.replace(old, new, 1)


def main():
    if len(sys.argv) != 2:
        fail("usage: patch_host_cb_dmabuf_modifier.py <source-root>")

    root = Path(sys.argv[1])
    ops_path = root / OPS
    ops_h_path = root / OPS_H
    dec_path = root / DEC
    for path in (ops_path, ops_h_path, dec_path):
        if not path.is_file():
            fail(f"missing {path}")

    ops = ops_path.read_bytes()
    ops_h = ops_h_path.read_bytes()
    dec = dec_path.read_bytes()

    for marker in MARKERS:
        for name, data in (("vk_common_operations.cpp", ops),
                           ("vk_common_operations.h", ops_h),
                           ("vk_decoder_global_state.cpp", dec)):
            if data.count(marker) != 0:
                fail(f"{name} already carries '{marker.decode()}'; refusing to patch twice")

    # D1 must be applied first: every anchor below pins D1 output.
    if ops.count(b"GFXDIAG: D1 P2D-IMAGE-EXT") != 1:
        fail("expected one D1 P2D-IMAGE-EXT trace before patching (D1 applied?)")
    if ops.count(b"GFXDIAG: D1 IMAGE-CREATE-FALLBACK") != 1:
        fail("expected one D1 IMAGE-CREATE-FALLBACK trace before patching (D1 applied?)")
    if ops.count(b"GFXDIAG: D1 IMAGE-SIZE-ZERO") != 1:
        fail("expected one D1 IMAGE-SIZE-ZERO trace before patching (D1 applied?)")
    if ops_h.count(b"p2dExternalMode") != 1:
        fail("expected one p2dExternalMode field before patching (D1 applied?)")
    if dec.count(b"p2dBridgeDmaBuf") != 4:
        fail("expected four p2dBridgeDmaBuf decisions before patching (D1 applied?)")

    ops = replace_once(ops, EXT_DECL_OLD, EXT_DECL_NEW, "modifier list declaration")
    ops = replace_once(ops, TILING_OLD, TILING_NEW, "ColorBuffer tiling")
    ops = replace_once(ops, EXT_CHAIN_OLD, EXT_CHAIN_NEW, "modifier pNext chain")
    ops = replace_once(ops, CREATE_FALLBACK_OLD, CREATE_FALLBACK_NEW,
                       "create fallback tiling reset")
    ops = replace_once(ops, SIZE_FALLBACK_OLD, SIZE_FALLBACK_NEW,
                       "size fallback tiling reset")
    ops = replace_once(ops, BITS_OLD, BITS_NEW, "image requirement trace")
    ops = replace_once(ops, EXPORT_OK_OLD, EXPORT_OK_NEW, "export trace")
    dec = replace_once(dec, CIB_IMAGE_OLD, CIB_IMAGE_NEW, "CIB1 alias modifier list")

    checks = [
        (ops.count(b"GFXDIAG: D4B P2D-IMAGE-MOD"), 1, "D4B image modifier trace"),
        (ops.count(b"GFXDIAG: D4B P2D-IMAGE-BITS"), 1, "D4B image requirement trace"),
        (ops.count(b"GFXDIAG: D4B P2D-DMABUF-EXPORT"), 1, "D4B export trace"),
        (ops.count(b"imageCi->tiling = VK_IMAGE_TILING_OPTIMAL"), 2,
         "both fallback tiling resets"),
        (ops.count(b"VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT"), 1,
         "ColorBuffer modifier tiling assignment"),
        (ops.count(b"VkImageDrmFormatModifierListCreateInfoEXT"), 1,
         "ColorBuffer modifier list declaration"),
        (ops.count(b"DRM_FORMAT_MOD_LINEAR"), 3, "ColorBuffer modifier log strings"),
        (ops.count(b"GFXDIAG: D1 P2D-IMAGE-EXT"), 1, "D1 image trace preserved"),
        (ops.count(b"GFXDIAG: D1 IMAGE-CREATE-FALLBACK"), 1,
         "D1 create fallback preserved"),
        (ops.count(b"GFXDIAG: D1 IMAGE-SIZE-ZERO"), 1, "D1 size fallback preserved"),
        (ops.count(b"GFXDIAG: D1 SKIP-AHB"), 1, "D1 AHB skip preserved"),
        (ops.count(b"GFXDIAG: D1 ALLOC-DMABUF"), 1, "D1 dma-buf allocation preserved"),
        (ops.count(b"GFXDIAG: D1 P2D-DMABUF-EXPORT"), 2, "D1 export traces preserved"),
        (dec.count(b"GFXDIAG: D4B CIB1-IMAGE-MOD"), 1, "D4B CIB1 modifier trace"),
        (dec.count(b"VkImageDrmFormatModifierListCreateInfoEXT"), 1,
         "CIB1 modifier list declaration"),
        (dec.count(b"p2dBridgeModifierTiling"), 4, "CIB1 modifier decisions"),
        (dec.count(b"GFXDIAG: D1 CIB1-IMAGE-EXT"), 1, "D1 CIB1 trace preserved"),
        (dec.count(b"GFXDIAG: D1 ZERO-SIZE-ALLOW"), 1, "D1 zero-size whitelist preserved"),
        (dec.count(b"p2dBridgeDmaBuf"), 4, "D1 CIB1 dma-buf decisions preserved"),
    ]
    for actual, expected, description in checks:
        if actual != expected:
            fail(f"post-check {description}: count {actual}, expected {expected}")
    if ops.count(b"STREAM_HANDLE_TYPE_MEM_DMABUF") < 6:
        fail("post-check D1 MEM_DMABUF handle occurrences: fewer than 6")

    if ops == ops_path.read_bytes():
        fail("vk_common_operations.cpp replacement produced no change")
    if dec == dec_path.read_bytes():
        fail("vk_decoder_global_state.cpp replacement produced no change")

    ops_path.write_bytes(ops)
    dec_path.write_bytes(dec)

    ops_line = ops[: ops.index(b"GFXDIAG: D4B P2D-IMAGE-MOD")].count(b"\n") + 1
    dec_line = dec[: dec.index(b"GFXDIAG: D4B CIB1-IMAGE-MOD")].count(b"\n") + 1
    print(f"patch_host_cb_dmabuf_modifier: patched {ops_path} at line {ops_line} "
          f"and {dec_path} at line {dec_line}")


if __name__ == "__main__":
    main()
