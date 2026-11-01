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

# Append the runtime override after launcher-provided features, as in the local
# mali-compat reference. This leaves the kernel 6.6 default policy unchanged.
edit(
    "host/virtio_gpu_gfxstream_renderer.cpp",
    '    for (const std::string& rendererFeature : gfxstream::Split(rendererFeatures, ",;")) {',
    '''    // Allow explicit bring-up overrides on kernels outside the default policy gate.
    std::string effectiveRendererFeatures = rendererFeatures;
    {
        const std::string envFeatures =
            gfxstream::base::getEnvironmentVariable("GFXSTREAM_RENDERER_FEATURES");
        if (!envFeatures.empty()) {
            if (!effectiveRendererFeatures.empty()) {
                effectiveRendererFeatures += ",";
            }
            effectiveRendererFeatures += envFeatures;
            GFXSTREAM_INFO("Applying renderer features from GFXSTREAM_RENDERER_FEATURES: %s",
                           envFeatures.c_str());
        }
    }

    for (const std::string& rendererFeature : gfxstream::Split(effectiveRendererFeatures, ",;")) {''',
)

# NDK CMake uses the syscall branch. Both entry points must create sealable
# memfds; seal only shared memory, after sizing it, not arbitrary file backing.
edit(
    "common/base/SharedMemory_posix.cpp",
    '#include "gfxstream/EintrWrapper.h"',
    '''#if defined(__linux__) || defined(__ANDROID__)
#ifndef MFD_CLOEXEC
#define MFD_CLOEXEC 0x0001U
#endif
#ifndef MFD_ALLOW_SEALING
#define MFD_ALLOW_SEALING 0x0002U
#endif
#endif

#include "gfxstream/EintrWrapper.h"''',
)
edit(
    "common/base/SharedMemory_posix.cpp",
    'mFd = syscall(__NR_memfd_create, mName.c_str(), FD_CLOEXEC);',
    'mFd = syscall(__NR_memfd_create, mName.c_str(), MFD_CLOEXEC | MFD_ALLOW_SEALING);',
)
edit(
    "common/base/SharedMemory_posix.cpp",
    '''#if defined(HAVE_MEMFD_CREATE)
        // DroidVM: seal against SHRINK only (protects existing mappings from SIGBUS), NOT GROW/SEAL.
        // The VMM (crosvm) grows a host-visible blob's shmem to a 2MB multiple to fold it into
        // order-9 folios (host-visible blob backing; see HostVisibleFolio.h). Only crosvm holds the
        // fd -- the guest reaches the memory through a BAR mapping, never the fd -- so allowing grow
        // does not widen the guest's authority. Growing is additive and never invalidates the
        // original-size mappings other consumers hold.
        if (fcntl(mFd, F_ADD_SEALS, F_SEAL_SHRINK) == -1) {
            err = -errno;
            close();
            return err;
        }
#endif''',
    '''#if defined(__linux__) || defined(__ANDROID__)
        // udmabuf requires SHRINK sealing. Keep mappings writable and avoid SEAL_SEAL.
        if (mShareType == ShareType::SHARED_MEMORY &&
            fcntl(mFd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW) == -1) {
            err = -errno;
            close();
            return err;
        }
#endif''',
)

# Log RingBlob export results without changing handle ownership or return values.
edit(
    "host/virtio_gpu_resource.cpp",
    '''            ShouldPinRingBlobsForGunyah() ? memory->dupHandle() : memory->releaseHandle();
#ifdef _WIN32''',
    '''            ShouldPinRingBlobsForGunyah() ? memory->dupHandle() : memory->releaseHandle();
        const int ringExportErrno = errno;
#ifdef _WIN32''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''        outHandle->handle_type = STREAM_HANDLE_TYPE_MEM_SHM;
        return 0;''',
    '''        outHandle->handle_type = STREAM_HANDLE_TYPE_MEM_SHM;
        GFXSTREAM_WARNING(
            "RINGBLOB-EXPORT: res=%u type=0x%x fd=%lld errno=%d",
            mId, outHandle->handle_type, (long long)outHandle->os_handle,
            handle == gfxstream::base::SharedMemory::invalidHandle() ? ringExportErrno : 0);
        errno = ringExportErrno;
        return 0;''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''        if (!memory->isExportable()) {
            return -EINVAL;
        }''',
    '''        if (!memory->isExportable()) {
            const int savedErrno = errno;
            GFXSTREAM_WARNING(
                "RINGBLOB-EXPORT: res=%u type=0x%x fd=-1 ret=%d errno=%d not-exportable",
                mId, STREAM_HANDLE_TYPE_MEM_SHM, -EINVAL, savedErrno);
            errno = savedErrno;
            return -EINVAL;
        }''',
)

# Android AHB is not an fd. Observe its actual stream type and opaque value;
# do not dup, acquire, release, reject, or reinterpret it as an fd here.
edit(
    "host/virtio_gpu_resource.cpp",
    '''    auto rawDescriptor = descriptorInfo.handle;
    switch (descriptorInfo.streamHandleType) {''',
    '''    auto rawDescriptor = descriptorInfo.handle;
    const int entryErrno = errno;
    GFXSTREAM_WARNING("FILL-EXPORT: type=0x%x input_handle=%lld errno=%d (AHB is not fd)",
                      descriptorInfo.streamHandleType, (long long)rawDescriptor, entryErrno);
    errno = entryErrno;
    switch (descriptorInfo.streamHandleType) {''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''            if (static_cast<int>(rawDescriptor) < 0) {
                return -EINVAL;
            }''',
    '''            if (static_cast<int>(rawDescriptor) < 0) {
                const int savedErrno = errno;
                GFXSTREAM_WARNING("FILL-EXPORT: type=0x%x fd=%lld ret=%d errno=%d",
                                  descriptorInfo.streamHandleType, (long long)rawDescriptor,
                                  -EINVAL, savedErrno);
                errno = savedErrno;
                return -EINVAL;
            }''',
)
edit(
    "host/virtio_gpu_resource.cpp",
    '''    outHandle->handle_type = descriptorInfo.streamHandleType;
    return 0;''',
    '''    outHandle->handle_type = descriptorInfo.streamHandleType;
#ifdef __ANDROID__
    const int exportErrno = errno;
    GFXSTREAM_WARNING("FILL-EXPORT: type=0x%x output_handle=%lld ret=0 errno=0",
                      outHandle->handle_type, (long long)outHandle->os_handle);
    errno = exportErrno;
#endif
    return 0;''',
)

# external_memory.cpp already defaults to AndroidAHB: verify, do not modify it.
platform_default = (root / "host/vulkan/external_memory.cpp").read_text(encoding="utf-8")
if """#elif defined(__ANDROID__)
    std::array<Mode, 1> supportedModes = {
        Mode::AndroidAHB,
    };""" not in platform_default:
    raise SystemExit("Android platform default is not the expected AndroidAHB")
print("verified AndroidAHB platform default; done")
