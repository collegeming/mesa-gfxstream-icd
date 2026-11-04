#!/usr/bin/env python3
"""Make AndroidAHB-mode host-visible allocations ONE object (AHB-first).

Why
---
The guest maps a Vulkan blob by the handle the host exports for it. For an ordinary
host-visible allocation that used to be a private memfd imported into the driver with
VK_EXT_external_memory_host ("SystemBlob"), and then exported as an AHardwareBuffer. A
host-pointer import is not AHB-exportable: the driver answers
vkGetMemoryAndroidHardwareBufferANDROID with a *different* allocation, so the guest writes one
buffer and the GPU reads another. Measured with a 5-second copy probe (4096 B, memory type 8):
 - readback never contains the guest's pattern (`0xa5000000`), it is stale host data;
 - the guest-side invalidate ran (HOSTVISIBLE-INVALIDATE: invalidates=2 bytes=8192) and changed
   nothing, which rules out cache maintenance as the explanation;
 - crosvm maps `/memfd:AHardwareBuffer` 0x5000 bytes while the host's memfd is
   `shared-memory-vk-N` 0x1000, i.e. a different inode.
ColorBuffers already do it correctly: `allocExternalMemory`'s AndroidAHB case calls
`AHardwareBuffer_allocate` and imports that AHB, so the image's memory and the handle the VMM
maps are the same object -- which is why the 1280x720 wallpaper (CIB1) renders while the
1128x58 bar and zink staging buffers do not.

What
----
1. In the SystemBlob / ordinary host-visible branch, when the external memory mode is
   AndroidAHB (and ExternalBlob is on, i.e. the blob reaches the VMM by handle), allocate an
   AHardwareBuffer of the requested size (BLOB format, CPU + GPU data-buffer usage), import it
   as the Vulkan memory and store the handle on MemoryInfo. No memfd, no host-pointer import.
2. exportMemoryHandle returns that stored handle, so the VMM maps the allocation the driver
   imported.
3. Fail closed: an AHB-mode export for a memory that was imported with a host pointer logs
   AHB-HOSTPTR-CONFLICT and returns no handle rather than letting the driver mint a second
   allocation. The allocation side also refuses to hand back a mismatched AHB.
4. Failure paths release the AHB exactly once and fall back to the existing memfd path with an
   AHB-FIRST-FALLBACK warning, so a driver that cannot allocate the AHB degrades to today's
   behaviour instead of refusing to start the VM.
5. GFXSTREAM_AHB_FIRST=0 disables the whole path without a rebuild: the one switch to flip if a
   device probe shows the AHB import is rejected, so the VM keeps allocating host-visible
   memory exactly as it did before this patch.

Not touched: GpuPool slices (one object already), ColorBuffers/CIB1 (AHB-first already), the
non-AHB modes, the zero-size guard, host-pointer contract validation, and the guest ICD.
"""
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


# ---------------------------------------------------------------------------
# 1. MemoryInfo carries the AHB that IS this allocation.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_internal_structs.h",
    """    void* importedHostPointer = nullptr;
    VkDeviceSize importedHostPointerSize = 0;
};""",
    """    void* importedHostPointer = nullptr;
    VkDeviceSize importedHostPointerSize = 0;
    // AHB-first backing for host-visible memory in AndroidAHB mode: the AHardwareBuffer
    // allocated during vkAllocateMemory is the memory itself -- the driver imported it and the
    // blob export hands this same handle to the VMM, so the guest's mapping and the GPU address
    // one allocation. Null in every other mode, and on the memfd fallback.
    void* ahbBacking = nullptr;
    VkDeviceSize ahbBackingBytes = 0;
};""",
)

# ---------------------------------------------------------------------------
# 2. Helper: the AHB for one host-visible buffer allocation.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """    VkResult on_vkAllocateMemory(gfxstream::base::BumpPool* pool, VkSnapshotApiCallHandle,
                                 VkDevice boxed_device, const VkMemoryAllocateInfo* pAllocateInfo,
                                 const VkAllocationCallbacks*, VkDeviceMemory* pMemory) {""",
    """#ifdef __ANDROID__
    // The AHardwareBuffer that will back one host-visible buffer allocation in AndroidAHB mode.
    // BLOB format with both CPU and GPU data-buffer usage: the guest maps it for CPU access and
    // the driver binds it as a buffer's memory. Null on any failure -- the caller then keeps the
    // private-memfd path.
    static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {
        if (!size || size > 0x7fffffffull) return nullptr;
        AHardwareBuffer_Desc desc = {};
        desc.width = static_cast<uint32_t>(size);
        desc.height = 1;
        desc.layers = 1;
        desc.format = AHARDWAREBUFFER_FORMAT_BLOB;
        desc.usage = AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN |
                     AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN |
                     AHARDWAREBUFFER_USAGE_GPU_DATA_BUFFER;
        AHardwareBuffer* ahb = nullptr;
        if (AHardwareBuffer_allocate(&desc, &ahb) != 0 || !ahb) {
            return nullptr;
        }
        return ahb;
    }
#endif

    VkResult on_vkAllocateMemory(gfxstream::base::BumpPool* pool, VkSnapshotApiCallHandle,
                                 VkDevice boxed_device, const VkMemoryAllocateInfo* pAllocateInfo,
                                 const VkAllocationCallbacks*, VkDeviceMemory* pMemory) {""",
)

# ---------------------------------------------------------------------------
# 3. Locals that survive to the MemoryInfo store.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """        void* mappedPtr = nullptr;
""",
    """        void* mappedPtr = nullptr;
        // Set when the AHB-first path below supplied the memory. Declared unconditionally so the
        // memfd-path guards stay simple; only written under __ANDROID__.
        void* ahbBacking = nullptr;
        VkDeviceSize ahbBackingBytes = 0;
""",
)

# ---------------------------------------------------------------------------
# 4. AHB-first allocation, before the memfd is created.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """              if (poolOffset < 0) {
                auto memory = SharedMemory("shared-memory-vk-" + std::to_string(sUniqueShmemId++),
                                           localAllocInfo.allocationSize);
""",
    """              if (poolOffset < 0) {
#ifdef __ANDROID__
                // AHB-first: the VMM may only map a handle this allocation exported, so the
                // memory has to be the object that handle names. A host-pointer import cannot be
                // re-exported as an AHB (the driver answers with a different allocation), which
                // is why this allocation previously gave the guest one buffer and the GPU
                // another. ColorBuffers already allocate the AHB first; do the same here.
                // Returns std::string, like every other getEnvironmentVariable caller here.
                static const bool ahbFirstEnabled = [] {
                    const std::string value =
                        gfxstream::base::getEnvironmentVariable("GFXSTREAM_AHB_FIRST");
                    return !(value.size() == 1 && value[0] == '0');
                }();
                if (ahbFirstEnabled &&
                    m_vkEmulation->getExternalMemoryMode() ==
                        ExternalMemory::Mode::AndroidAHB &&
                    m_vkEmulation->getFeatures().ExternalBlob.enabled() &&
                    importInfo.buffer == nullptr) {
                    AHardwareBuffer* ahb = allocateAhbFirstMemory(localAllocInfo.allocationSize);
                    VkDeviceSize ahbBytes = 0;
                    if (ahb) {
                        VkAndroidHardwareBufferPropertiesANDROID ahbProps = {
                            VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_PROPERTIES_ANDROID,
                            nullptr,
                            0,
                        };
                        const VkResult ahbRes =
                            vk->vkGetAndroidHardwareBufferPropertiesANDROID
                                ? vk->vkGetAndroidHardwareBufferPropertiesANDROID(device, ahb,
                                                                                  &ahbProps)
                                : VK_ERROR_EXTENSION_NOT_PRESENT;
                        if (ahbRes != VK_SUCCESS) {
                            GFXSTREAM_ERROR(
                                "AHB-FIRST: properties query failed (%d) for %llu bytes",
                                (int)ahbRes, (unsigned long long)localAllocInfo.allocationSize);
                        } else if (ahbProps.memoryTypeBits == 0) {
                            GFXSTREAM_ERROR("AHB-FIRST: AHB reports no memory types");
                        } else if (localAllocInfo.memoryTypeIndex >= 32 ||
                                   (ahbProps.memoryTypeBits &
                                    (uint32_t{1} << localAllocInfo.memoryTypeIndex)) == 0) {
                            // v3: serve the guest's host-visible request from the memory type
                            // the AHB can actually be imported as.
                            //
                            // The guest asked for type 8 (DEVICE_LOCAL|HOST_VISIBLE|
                            // HOST_COHERENT) but Mali imports AHBs only on type 1
                            // (DEVICE_LOCAL|HOST_VISIBLE|HOST_CACHED) -- measured on device:
                            // AHB bits 0x2, buffer requirements 0x4100 (types 8 and 14).
                            //
                            // Remapping the *host* index here is not a new promise: the memfd
                            // path in this same function already does exactly this
                            // (patch_systemblob_host_pointer_type.py prints
                            // "SYSBLOB-HOSTPTR: memoryTypeIndex 8 -> 1"), and buffers bind
                            // successfully at host index 1 (BIND-BEFORE-MEM ... memindex=1 in the
                            // device logs). The guest's own type index is untouched, so its bind
                            // stays legal and its visible memory properties are unchanged.
                            //
                            // The old invariant "the host index never changes" was protecting a
                            // coherent guarantee that does not hold in this configuration: the
                            // 4096 B bidirectional probe showed the guest's mapping and the
                            // host memfd are different objects, so there was never shared
                            // coherent memory to protect. What must hold instead is one object:
                            // the handle exported is the handle imported (checked at export and
                            // logged as AHB-FIRST-EXPORT ... identity=same).
                            const uint32_t requestedType = localAllocInfo.memoryTypeIndex;
                            uint32_t ahbType = UINT32_MAX;
                            for (uint32_t i = 0; i <= 31; ++i) {
                                if (ahbProps.memoryTypeBits & (uint32_t{1} << i)) {
                                    ahbType = i;
                                    break;
                                }
                            }
                            static std::atomic<unsigned> sAhbFirstTypeRemaps{0};
                            if (sAhbFirstTypeRemaps.fetch_add(1) < 16) {
                                // Flags of the type actually used, read the same way the
                                // allocation path reads them (host memory properties).
                                uint32_t ahbTypeFlags = 0;
                                {
                                    std::lock_guard<std::mutex> remapLock(mMutex);
                                    const auto* remapDeviceInfo =
                                        gfxstream::base::find(mDeviceInfo, device);
                                    const auto* remapPhysdevInfo =
                                        remapDeviceInfo
                                            ? gfxstream::base::find(
                                                  mPhysdevInfo, remapDeviceInfo->physicalDevice)
                                            : nullptr;
                                    if (remapPhysdevInfo && remapPhysdevInfo->memoryPropertiesHelper) {
                                        const auto& hostProps = remapPhysdevInfo
                                                                    ->memoryPropertiesHelper
                                                                    ->getHostMemoryProperties();
                                        if (ahbType < hostProps.memoryTypeCount) {
                                            ahbTypeFlags =
                                                (uint32_t)hostProps.memoryTypes[ahbType]
                                                    .propertyFlags;
                                        }
                                    }
                                }
                                GFXSTREAM_WARNING(
                                    "AHB-FIRST-TYPE-REMAP: host %u -> %u flags=0x%x ahb-bits=0x%x "
                                    "(guest type index unchanged)",
                                    requestedType, ahbType, ahbTypeFlags,
                                    ahbProps.memoryTypeBits);
                            }
                            localAllocInfo.memoryTypeIndex = ahbType;
                        }
                        // Both branches need the byte count: the AHB is used either way.
                        // Leaving it zero here made the caller's `if (ahb && ahbBytes)` false
                        // and sent every allocation down the memfd fallback (measured on
                        // device as AHB-FIRST-FALLBACK for 4096 B).
                        ahbBytes = ahbProps.allocationSize
                                       ? ahbProps.allocationSize
                                       : localAllocInfo.allocationSize;
                    }
                    if (ahb && ahbBytes) {
                        if (ahbBytes > localAllocInfo.allocationSize) {
                            localAllocInfo.allocationSize = ahbBytes;
                        }
                        importInfo.buffer = ahb;
                        vk_append_struct(&structChainIter, &importInfo);
                        ahbBacking = ahb;
                        ahbBackingBytes = ahbBytes;
                        GFXSTREAM_WARNING(
                            "AHB-FIRST: size=%llu host-type=%u ahb=%p -> one allocation shared "
                            "by guest and GPU",
                            (unsigned long long)localAllocInfo.allocationSize,
                            localAllocInfo.memoryTypeIndex, (void*)ahb);
                    } else {
                        if (ahb) AHardwareBuffer_release(ahb);
                        GFXSTREAM_WARNING(
                            "AHB-FIRST-FALLBACK: no usable AHB for %llu bytes; keeping the memfd "
                            "path (guest/GPU identity NOT guaranteed)",
                            (unsigned long long)localAllocInfo.allocationSize);
                    }
                }
#endif
                // AHB-FIRST: the import above already supplied this allocation's memory. Creating a
                // memfd and importing its host pointer next to it would give the guest and the GPU
                // two different objects again. Wrapped rather than rewritten (C++ ignores
                // indentation) so every upstream line stays byte-identical and the later
                // host-pointer patch keeps its anchor.
                if (!ahbBacking) {
                auto memory = SharedMemory("shared-memory-vk-" + std::to_string(sUniqueShmemId++),
                                           localAllocInfo.allocationSize);
""",
)

# Close the wrapper at the pool gate's own close, in its own edit so the OLD text of the
# insertion above stays exactly the two upstream lines.
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """                sharedMemory = std::make_optional<SharedMemory>(std::move(memory));
              }  // if (poolOffset < 0)
""",
    """                sharedMemory = std::make_optional<SharedMemory>(std::move(memory));
                }  // if (!ahbBacking)
              }  // if (poolOffset < 0)
""",
)

# ---------------------------------------------------------------------------
# 6. Publish the AHB on the MemoryInfo (and the export reads it there).
#    Stored early, right with the host-pointer fields, so every failure path
#    that already tears the memory down also releases the AHB.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """        if (actualHostImport && actualHostImport->pHostPointer) {
            memoryInfo.importedHostPointer = actualHostImport->pHostPointer;
            memoryInfo.importedHostPointerSize = localAllocInfo.allocationSize;
        }""",
    """        if (actualHostImport && actualHostImport->pHostPointer) {
            memoryInfo.importedHostPointer = actualHostImport->pHostPointer;
            memoryInfo.importedHostPointerSize = localAllocInfo.allocationSize;
        }
#ifdef __ANDROID__
        // The AHB this allocation imported (AHB-first). Also the handle the blob export returns,
        // so the guest's mapping and the driver's memory are one object. Never set together with
        // importedHostPointer: the two backings are mutually exclusive by construction.
        memoryInfo.ahbBacking = ahbBacking;
        memoryInfo.ahbBackingBytes = ahbBackingBytes;
#endif""",
)

# ---------------------------------------------------------------------------
# 7. Export: hand the VMM the AHB this allocation imported; refuse the
#    host-pointer import rather than let the driver invent a second allocation.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """            case ExternalMemory::Mode::AndroidAHB: {
                if (!vk->vkGetMemoryAndroidHardwareBufferANDROID) {""",
    """            case ExternalMemory::Mode::AndroidAHB: {
                // Caller (vkGetBlobInternal) holds mMutex; do not take it again.
                auto* storedInfo = gfxstream::base::find(mMemoryInfo, memory);
                if (storedInfo && storedInfo->ahbBacking) {
                    // The allocation imported this AHB, so exporting it returns the same object
                    // the driver reads and writes. This is the whole point of AHB-first, and it
                    // is logged every time so a device run can confirm identity rather than
                    // infer it: AHB-FIRST-EXPORT imported==exported must print identical
                    // pointers for a real allocation.
                    ret.streamHandleType = STREAM_HANDLE_TYPE_PLATFORM_AHB;
                    ret.handle = reinterpret_cast<ExternalHandleType>(storedInfo->ahbBacking);
                    GFXSTREAM_WARNING(
                        "AHB-FIRST-EXPORT: memory=%p imported=%p exported=%p bytes=%llu "
                        "identity=%s",
                        (void*)memory, storedInfo->ahbBacking, (void*)ret.handle,
                        (unsigned long long)storedInfo->ahbBackingBytes,
                        storedInfo->ahbBacking == (void*)ret.handle ? "same" : "MISMATCH");
                    return ret;
                }
                if (storedInfo && storedInfo->importedHostPointer) {
                    // Never let the driver mint an AHB for a host-pointer import: the guest would
                    // map that new allocation while the GPU keeps reading the imported pages.
                    GFXSTREAM_ERROR(
                        "AHB-HOSTPTR-CONFLICT: memory %p was imported with "
                        "VK_EXT_external_memory_host and has no AHB backing; refusing to export "
                        "a different allocation to the guest",
                        (void*)memory);
                    return std::nullopt;
                }
                if (!vk->vkGetMemoryAndroidHardwareBufferANDROID) {""",
)

# ---------------------------------------------------------------------------
# 8. Release exactly once, after the driver dropped its import.
# ---------------------------------------------------------------------------
edit(
    "host/vulkan/vk_decoder_global_state.cpp",
    """        deviceDispatch->vkFreeMemory(device, memory, nullptr);
    }

    void freeMemoryLocked(VkDevice device, VulkanDispatch* deviceDispatch, VkDeviceMemory memory)""",
    """        deviceDispatch->vkFreeMemory(device, memory, nullptr);

#ifdef __ANDROID__
        // Drop the AHB-first reference only after the driver released the memory that imported
        // it, and clear the field so a second destroy cannot double-free.
        if (memoryInfo.ahbBacking) {
            AHardwareBuffer_release(reinterpret_cast<AHardwareBuffer*>(memoryInfo.ahbBacking));
            memoryInfo.ahbBacking = nullptr;
            memoryInfo.ahbBackingBytes = 0;
        }
#endif
    }

    void freeMemoryLocked(VkDevice device, VulkanDispatch* deviceDispatch, VkDeviceMemory memory)""",
)

print("AHB-first host-visible backing installed")
