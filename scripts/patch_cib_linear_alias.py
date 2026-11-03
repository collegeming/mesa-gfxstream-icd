#!/usr/bin/env python3
"""CIB1: make the AHB alias image LINEAR so its memory requirements intersect the
AHB-importable memory type.  Applied after the existing CIB patches.

Recorded device facts (Mali):
  * vkGetAndroidHardwareBufferPropertiesANDROID memoryTypeBits = 0x2 (type 1 only).
  * The OPTIMAL ColorBuffer alias image reports requirements 0x1040 ({6,12}), so
    AHB bits 0x2 and image bits 0x1040 have an empty intersection.
  * Type 1 is DEVICE_LOCAL|HOST_VISIBLE|HOST_CACHED; linear host-visible images
    belong to that cached group, while the OPTIMAL {6,12} group is not importable.

Change: when ClassicImageBridge is enabled and the external memory mode is
AndroidAHB, create the ColorBuffer host image with VK_IMAGE_TILING_LINEAR.  The
alias image is derived from that create info, and an explicit
DRM_FORMAT_MOD_LINEAR guest modifier is accepted for it.  Format, usage and
create flags stay exactly what the optimal factory selected (niri: fmt 44/50,
usage 0x97, MUTABLE_FORMAT / EXTENDED_USAGE), and the LINEAR path is used only
when the linear format features cover every one of those usage bits; otherwise
the previous OPTIMAL image is kept unchanged.  The AHB allocation (format/usage/
allocationSize from the AHB query, usage 0x300) and the AHB-RETAIN /
AHB-SIZE-MISMATCH guards are not touched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
root = parser.parse_args().root.resolve()


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'{rel}: expected one anchor ({text.count(old)}): {old[:110]}')
    path.write_text(text.replace(old, new), encoding='utf-8')


ops = 'host/vulkan/vk_common_operations.cpp'
dec = 'host/vulkan/vk_decoder_global_state.cpp'

# 1. ColorBuffer host image: derive LINEAR from the same factory while keeping the
#    optimal usage/flags, so the guest-visible contract does not shrink.
edit(ops, '''    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
                               ? VK_IMAGE_TILING_LINEAR
                               : VK_IMAGE_TILING_OPTIMAL;
    std::unique_ptr<VkImageCreateInfo> imageCi = generateColorBufferVkImageCreateInfoLocked(
        vkFormat, infoPtr->width, infoPtr->height, tiling, mipLevels);
    // pNext will be filled later.
    if (imageCi == nullptr) {''', '''    VkImageTiling tiling = (infoPtr->memoryProperty & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
                               ? VK_IMAGE_TILING_LINEAR
                               : VK_IMAGE_TILING_OPTIMAL;
    std::unique_ptr<VkImageCreateInfo> imageCi = generateColorBufferVkImageCreateInfoLocked(
        vkFormat, infoPtr->width, infoPtr->height, tiling, mipLevels);
    // CIB1-LINEAR-ALIAS: the AHB-backed ColorBuffer is the linear scanout buffer the
    // guest asked for.  A LINEAR host image reports memory types that include the
    // AHB-importable cached host-visible type 1, unlike the optimal-image {6,12}
    // group (recorded: AHB bits 0x2 vs image bits 0x1040).
    const bool cibLinearAlias = mFeatures.ClassicImageBridge.enabled() &&
                                getExternalMemoryMode() == ExternalMemory::Mode::AndroidAHB;
    if (cibLinearAlias && imageCi != nullptr) {
        std::unique_ptr<VkImageCreateInfo> linearCi = generateColorBufferVkImageCreateInfoLocked(
            vkFormat, infoPtr->width, infoPtr->height, VK_IMAGE_TILING_LINEAR, mipLevels);
        // Keep the optimal usage set (niri: 0x97) and the factory-selected flags.
        // Only use LINEAR when its format features cover every usage bit.
        const bool linearCovers = linearCi != nullptr &&
                                  (imageCi->usage & ~linearCi->usage) == 0;
        static std::atomic<uint32_t> cibLinearAliasLogs{0};
        if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1" &&
            cibLinearAliasLogs.fetch_add(1, std::memory_order_relaxed) < 24) {
            GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: color-buffer=%u tiling=%d usage=0x%x flags=0x%x "
                           "linear-usage=0x%x linear-covers=%d",
                           colorBufferHandle, linearCovers ? 1 : 0,
                           (unsigned)imageCi->usage, (unsigned)imageCi->flags,
                           linearCi ? (unsigned)linearCi->usage : 0u, linearCovers ? 1 : 0);
        }
        if (linearCovers) {
            linearCi->usage = imageCi->usage;
            imageCi = std::move(linearCi);
            tiling = VK_IMAGE_TILING_LINEAR;
        }
    }
    // pNext will be filled later.
    if (imageCi == nullptr) {''')

# 2. Bounded diagnostic at the AHB import decision: shows the AHB-reported layout,
#    mask and the image requirements mask without changing either.
edit(ops, '''                // Find a memory type that satisfies both the image and AHB requirements
                uint32_t combinedBits = ahbProps.memoryTypeBits &
                                        cbInfoPtr->imageMemReqs.memoryTypeBits;''', '''                {
                    static std::atomic<uint32_t> cibLinearAliasAhbLogs{0};
                    if (gfxstream::base::getEnvironmentVariable("GFXSTREAM_NIRI_IMAGE_TRACE") == "1" &&
                        cibLinearAliasAhbLogs.fetch_add(1, std::memory_order_relaxed) < 24) {
                        AHardwareBuffer_Desc ahbDesc{};
                        AHardwareBuffer_describe(ahb, &ahbDesc);
                        GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: resource=%u ahb=%ux%u format=%u "
                                       "ahb-usage=0x%llx size=%llu bits=0x%x image-tiling=%d "
                                       "image-usage=0x%x image-flags=0x%x image-bits=0x%x",
                                       cbInfoPtr->handle, ahbDesc.width, ahbDesc.height,
                                       ahbDesc.format, (unsigned long long)ahbDesc.usage,
                                       (unsigned long long)ahbProps.allocationSize,
                                       ahbProps.memoryTypeBits,
                                       (int)cbInfoPtr->imageCreateInfoShallow.tiling,
                                       (unsigned)cbInfoPtr->imageCreateInfoShallow.usage,
                                       (unsigned)cbInfoPtr->imageCreateInfoShallow.flags,
                                       cbInfoPtr->imageMemReqs.memoryTypeBits);
                    }
                }

                // Find a memory type that satisfies both the image and AHB requirements
                uint32_t combinedBits = ahbProps.memoryTypeBits &
                                        cbInfoPtr->imageMemReqs.memoryTypeBits;''')

# 3. Alias image: only a linear guest image is bridged, and the host alias is
#    created LINEAR.  Usage/flags stay from the ColorBuffer create info.
edit(dec, '''        VkImageCreateInfo ci = cb->imageCreateInfoShallow;
        if ((input->usage & ~ci.usage) || (input->flags & ~(ci.flags | VK_IMAGE_CREATE_ALIAS_BIT)))
            return fail("usage-flags", VK_ERROR_FORMAT_NOT_SUPPORTED);
        // Preserve compatible view-format semantics; reject unrelated or unsupported pNexts.
        const VkImageFormatListCreateInfo* list = nullptr;
        for (auto* n = static_cast<const VkBaseInStructure*>(input->pNext); n; n = n->pNext) {
            if (n->sType == VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID) continue;
            if (n->sType != VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO || list)
                return fail("view-list-pnext", VK_ERROR_FORMAT_NOT_SUPPORTED);
            list = reinterpret_cast<const VkImageFormatListCreateInfo*>(n);
        }''', '''        VkImageCreateInfo ci = cb->imageCreateInfoShallow;
        if ((input->usage & ~ci.usage) || (input->flags & ~(ci.flags | VK_IMAGE_CREATE_ALIAS_BIT)))
            return fail("usage-flags", VK_ERROR_FORMAT_NOT_SUPPORTED);
        // CIB1-LINEAR-ALIAS: honor only a linear guest image.  niri asks for LINEAR
        // tiling (or an explicit DRM_FORMAT_MOD_LINEAR modifier); the host alias must
        // be LINEAR so its requirements cover the AHB-importable cached type.  Any
        // other tiling stays fail-closed.
        const bool linearTiling = input->tiling == VK_IMAGE_TILING_LINEAR;
        const bool linearModifierTiling =
            input->tiling == VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
        if (!linearTiling && !linearModifierTiling)
            return fail("tiling", VK_ERROR_FORMAT_NOT_SUPPORTED);
        bool sawLinearModifier = false;
        // Preserve compatible view-format semantics; reject unrelated or unsupported pNexts.
        const VkImageFormatListCreateInfo* list = nullptr;
        for (auto* n = static_cast<const VkBaseInStructure*>(input->pNext); n; n = n->pNext) {
            if (n->sType == VK_STRUCTURE_TYPE_NATIVE_BUFFER_ANDROID) continue;
            if (n->sType == VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT) {
                const auto* explicitModifier =
                    reinterpret_cast<const VkImageDrmFormatModifierExplicitCreateInfoEXT*>(n);
                // DRM_FORMAT_MOD_LINEAR == 0, one plane, no offset: layout-compatible
                // with VK_IMAGE_TILING_LINEAR, so the host image drops the modifier.
                if (!sawLinearModifier && linearModifierTiling &&
                    explicitModifier->drmFormatModifier == 0 &&
                    explicitModifier->drmFormatModifierPlaneCount == 1 &&
                    explicitModifier->pPlaneLayouts &&
                    explicitModifier->pPlaneLayouts[0].offset == 0) {
                    sawLinearModifier = true;
                    continue;
                }
                return fail("modifier-pnext", VK_ERROR_FORMAT_NOT_SUPPORTED);
            }
            if (n->sType != VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO || list)
                return fail("view-list-pnext", VK_ERROR_FORMAT_NOT_SUPPORTED);
            list = reinterpret_cast<const VkImageFormatListCreateInfo*>(n);
        }
        if (linearModifierTiling && !sawLinearModifier)
            return fail("modifier-absent", VK_ERROR_FORMAT_NOT_SUPPORTED);
        ci.tiling = VK_IMAGE_TILING_LINEAR;''')

# 4. Alias success diagnostic: the effective tiling plus the ColorBuffer image and
#    AHB memory type masks.
edit(dec, '''        GFXSTREAM_INFO("CIB1: image=%p resource=%u bytes=%llu physical-type=%u shadow=%u unbound=1",
            (void*)*image, resource, (unsigned long long)cb->memory.size, cb->memory.typeIndex, shadow);
        return VK_SUCCESS;''', '''        GFXSTREAM_INFO("CIB1: image=%p resource=%u bytes=%llu physical-type=%u shadow=%u unbound=1",
            (void*)*image, resource, (unsigned long long)cb->memory.size, cb->memory.typeIndex, shadow);
        static std::atomic<uint32_t> cibLinearAliasImageLogs{0};
        if (diag && cibLinearAliasImageLogs.fetch_add(1, std::memory_order_relaxed) < 24) {
            GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: image=%p resource=%u tiling=%d usage=0x%x flags=0x%x "
                           "ahb-type=%u image-bits=0x%x",
                           (void*)*image, resource, (int)ci.tiling, (unsigned)ci.usage,
                           (unsigned)ci.flags, cb->memory.typeIndex,
                           cb->imageMemReqs.memoryTypeBits);
        }
        return VK_SUCCESS;''')

# 5. The advertised CIB capability must describe the same LINEAR image that the
#    creation path now produces.
edit(dec, '''        auto native = m_vkEmulation->generateColorBufferVkImageCreateInfo(input->format, 1, 1, VK_IMAGE_TILING_OPTIMAL, 1);''',
     '''        auto native = m_vkEmulation->generateColorBufferVkImageCreateInfo(input->format, 1, 1, VK_IMAGE_TILING_LINEAR, 1);''')

# Local post-conditions: both production sites carry the new marker, and the
# guarded AHB import text is unchanged.
common = (root / ops).read_text(encoding='utf-8')
decoder = (root / dec).read_text(encoding='utf-8')
assert common.count('CIB1-LINEAR-ALIAS') == 3, common.count('CIB1-LINEAR-ALIAS')
assert decoder.count('CIB1-LINEAR-ALIAS') == 2, decoder.count('CIB1-LINEAR-ALIAS')
assert 'GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: color-buffer=' in common
assert 'GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: resource=' in common
assert 'GFXSTREAM_INFO("CIB1-LINEAR-ALIAS: image=' in decoder
assert 'AHB-SIZE-MISMATCH: ahb=%llu image=%llu; refusing import' in common
assert 'AHB-RETAIN: keep the AHB reference for the alias allocation' in common
assert 'ci.usage =' not in decoder and 'ci.flags =' not in decoder
print('CIB1-LINEAR-ALIAS: LINEAR ColorBuffer/alias image, explicit LINEAR modifier accepted, '
      'AHB usage/format/size and guards unchanged')
