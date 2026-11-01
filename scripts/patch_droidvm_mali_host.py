#!/usr/bin/env python3
"""Apply only the DroidVM Mali diagnostic host semantic fixes."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()


def edit(rel, old, new):
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{rel}: expected one occurrence, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print("patched", rel)


# Do not advertise the command that crashes the guest Vulkan constructor.
edit(
    "host/virtio_gpu_frontend.cpp",
    "capset->hasSetMetadataCommand = 1;",
    "capset->hasSetMetadataCommand = 0;",
)

# For imported AHB allocations, the AHB properties supply size and memory types.
# Mali can report zero/disjoint pre-bind image requirements (0 or 0x1040 vs 0x2).
edit(
    "host/vulkan/vk_common_operations.cpp",
    """                if (!combinedBits) {
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Falling back to non-exportable allocation.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    AHardwareBuffer_release(ahb);
                    break;
                }""",
    """                if (!combinedBits) {
                    // Mali may report zero/disjoint image requirements before AHB import.
                    // Use the imported AHB's authoritative memory types and allocation size.
                    GFXSTREAM_WARNING(
                        "No common memory type for AHB (0x%x) and image (0x%x). "
                        "Using AHB memory type bits for import.",
                        ahbProps.memoryTypeBits, cbInfoPtr->imageMemReqs.memoryTypeBits);
                    combinedBits = ahbProps.memoryTypeBits;
                }""",
)

# Preserve both explicit environment and renderer-feature overrides.
edit(
    "host/virtio_gpu_gfxstream_renderer.cpp",
    """    // On Android the platform default for external memory is AndroidAHB and nothing else --
    // calculateMode()'s Android list is one entry long, with no fd fallback. That is wrong for
    // this route twice over. The host driver here is Mesa turnip, whose exports are dma-buf fds;
    // and every host-visible blob, the GpuPool and the guest-alloc slice are dma-buf all the way
    // down, so a ColorBuffer exported as an AHardwareBuffer cannot be handed to any of them. A
    // host that picks AHB advertises external memory it cannot actually share, and the guest
    // renders into memory nothing scans out.
    //
    // OpaqueFd is the mode that means "fd", and it upgrades itself to
    // VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT wherever the driver reports
    // VK_EXT_external_memory_dma_buf, which turnip does. Set before the feature string is parsed,
    // so an explicit VulkanExternalMemoryMode:... there still wins; GFXSTREAM_EXTERNAL_MEMORY_MODE
    // is the same escape hatch for a launcher that cannot reach the feature string.
    //
    // On a host driver with no fd export -- the closed Adreno blob -- this resolves to
    // NotSupported rather than silently falling back to AHB. That is deliberate: the AHB route
    // cannot back host-visible coherent memory on this stack at all, so failing where the mode is
    // chosen is far easier to read than a black screen several layers down.
    {
        const std::string modeOverride =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_EXTERNAL_MEMORY_MODE");
        const std::string mode = modeOverride.empty() ? "OpaqueFd" : modeOverride;
        if (!features.processFeatureString("VulkanExternalMemoryMode:" + mode,
                                           "DroidVM default: the host driver exports dma-buf")) {""",
    """    // Match the Android platform default for the Mali system Vulkan driver.
    // Explicit environment and renderer-feature overrides still take precedence.
    {
        const std::string modeOverride =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_EXTERNAL_MEMORY_MODE");
        const std::string mode = modeOverride.empty() ? "AndroidAHB" : modeOverride;
        if (!features.processFeatureString("VulkanExternalMemoryMode:" + mode,
                                           "DroidVM Mali default: AndroidAHB")) {""",
)

# external_memory.cpp already defaults to AndroidAHB: verify, do not modify it.
platform_default = (root / "host/vulkan/external_memory.cpp").read_text(encoding="utf-8")
if """#elif defined(__ANDROID__)
    std::array<Mode, 1> supportedModes = {
        Mode::AndroidAHB,
    };""" not in platform_default:
    raise SystemExit("Android platform default is not the expected AndroidAHB")
print("verified AndroidAHB platform default; done")
