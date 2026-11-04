#!/usr/bin/env python3
"""Assert the AHB-first host-visible invariants on a patched gfxstream tree.

Run after patch_ahb_first_hostvisible.py (CI runs it on the patched source before the build).

Checks:
  1. MemoryInfo carries ahbBacking/ahbBackingBytes next to the host-pointer fields, and the store
     sits where every teardown path can see it (with the host-pointer fields, not at the end);
  2. the AHB-first block is gated on AndroidAHB + ExternalBlob, allocates the AHB before the
     memfd exists, imports it, and records the handle;
  3. the memfd creation, the host-pointer import append and the SHARED_MEMORY publication are all
     guarded by `if (!ahbBacking)`, so an AHB allocation can never also carry a host pointer;
  4. exportMemoryHandle returns the stored handle before asking the driver, refuses a
     host-pointer import with AHB-HOSTPTR-CONFLICT, and does not take mMutex again;
  5. the AHB is released exactly once, after vkFreeMemory, and the field is cleared;
  6. the allocation failure path releases the AHB exactly once and falls back with a warning;
  7. unrelated guarantees are untouched: the zero-size guard, the host-pointer contract helper,
     the GpuPool branch, and the ColorBuffer AHB import.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

structs = (root / "host/vulkan/vk_decoder_internal_structs.h").read_text(
    encoding="utf-8", errors="replace").replace("\r\n", "\n")
state = (root / "host/vulkan/vk_decoder_global_state.cpp").read_text(
    encoding="utf-8", errors="replace").replace("\r\n", "\n")

# 1. MemoryInfo fields + early store.
assert "void* ahbBacking = nullptr;" in structs
assert "VkDeviceSize ahbBackingBytes = 0;" in structs
assert structs.index("void* importedHostPointer = nullptr;") < structs.index(
    "void* ahbBacking = nullptr;")
store = "        memoryInfo.ahbBacking = ahbBacking;"
assert state.count(store) == 1, "exactly one store site"
assert state.index("memoryInfo.importedHostPointer = actualHostImport->pHostPointer;") < \
    state.index(store), "store belongs with the host-pointer fields (early, teardown-visible)"
assert state.index(store) < state.index("memoryInfo.sharedMemory = std::exchange"), \
    "store must precede the late memfd publication bookkeeping"

# 2. The AHB-first block.
assert state.count("static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size)") == 1
helper = state[state.index("static AHardwareBuffer* allocateAhbFirstMemory("):]
helper = helper[:helper.index("#endif")]
assert "AHardwareBuffer_allocate(&desc, &ahb)" in helper
assert "AHARDWAREBUFFER_FORMAT_BLOB" in helper
assert "AHARDWAREBUFFER_USAGE_GPU_DATA_BUFFER" in helper
assert "AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN" in helper
gate = state.index("m_vkEmulation->getExternalMemoryMode() ==\n                        "
                   "ExternalMemory::Mode::AndroidAHB")
tail = state.index("auto memory = SharedMemory(\"shared-memory-vk-\"")
call = state.index("ahb = allocateAhbFirstMemory(localAllocInfo.allocationSize);")
assert gate < call < tail, "AHB allocation must run before the memfd is created"
assert call < state.index("importInfo.buffer = ahb;", call) < tail
import_append = state.index("importInfo.buffer = ahb;", call)
block = state[state.index("AHB-first: the VMM may only map a handle"):import_append]
# Kill switch: one env var restores the memfd path without a rebuild.
assert "GFXSTREAM_AHB_FIRST" in block
assert "value.size() == 1 && value[0] == '0'" in block
assert "const std::string value =" in block, "getEnvironmentVariable returns std::string"
# The AHB's own memory types are authoritative; the host index may move, the guest ABI does not.
assert "AHB-FIRST-TYPE-SWITCH" in block
assert "localAllocInfo.memoryTypeIndex = i;" in block
# No dedicated struct is appended here: the guest cannot name the buffer before bind, so an
# empty VkMemoryDedicatedAllocateInfo would be a no-op (and a diff with no effect).
assert "localDedicatedAllocInfo" not in block
assert "vk_append_struct(&structChainIter, &importInfo);" in state
assert "ahbBacking = ahb;" in state
assert "ahbBackingBytes = ahbBytes;" in state
assert "m_vkEmulation->getFeatures().ExternalBlob.enabled()" in state[gate:tail], \
    "AHB-first only when the blob reaches the VMM by handle"

# 3. No second backing next to the AHB.
memfd_block = state[state.index("} else if (m_vkEmulation->getFeatures().SystemBlob.enabled()) {"):]
memfd_block = memfd_block[:memfd_block.index("}  // if (poolOffset < 0)")]
assert "if (!ahbBacking) {" in memfd_block
assert memfd_block.index("if (!ahbBacking) {") < memfd_block.index("int ret = memory.create(0600);")
assert memfd_block.index("int ret = memory.create(0600);") < memfd_block.index(
    "importHostInfo.pHostPointer = mappedPtr;")
shared_pub = state[state.index("                // Same reason: an uncreated memfd"):
                   state.index("}  // if (poolOffset < 0)")]
assert "if (!ahbBacking) {" in shared_pub
assert shared_pub.index("if (!ahbBacking) {") < shared_pub.index(
    "sharedMemory = std::make_optional<SharedMemory>")

# 4. Export: stored handle first, conflict guard second, driver last; no relock.
export_fn = state[state.index("std::optional<BlobDescriptorType> exportMemoryHandle("):]
export_fn = export_fn[:export_fn.index("void getSupportedSemaphoreHandleTypes(")]
ahb_case = export_fn[export_fn.index("case ExternalMemory::Mode::AndroidAHB: {"):]
assert "auto* storedInfo = gfxstream::base::find(mMemoryInfo, memory);" in ahb_case
assert "if (storedInfo && storedInfo->ahbBacking) {" in ahb_case
assert "ret.handle = reinterpret_cast<ExternalHandleType>(storedInfo->ahbBacking);" in ahb_case
assert "AHB-HOSTPTR-CONFLICT" in ahb_case
assert "return std::nullopt;" in ahb_case[ahb_case.index("AHB-HOSTPTR-CONFLICT"):]
stored_ret = ahb_case.index("ret.handle = reinterpret_cast<ExternalHandleType>(storedInfo->ahbBacking);")
conflict = ahb_case.index("AHB-HOSTPTR-CONFLICT")
driver = ahb_case.index("vk->vkGetMemoryAndroidHardwareBufferANDROID(device, &getAhbInfo")
assert stored_ret < conflict < driver, "stored handle, then refusal, then the driver"
assert "lock_guard" not in ahb_case, "vkGetBlobInternal already holds mMutex; a second lock deadlocks"

# 5/6. Release exactly once, after vkFreeMemory, field cleared; failure path releases once.
assert state.count("AHardwareBuffer_release(reinterpret_cast<AHardwareBuffer*>(memoryInfo.ahbBacking));") == 1
release = state.index("AHardwareBuffer_release(reinterpret_cast<AHardwareBuffer*>(memoryInfo.ahbBacking));")
free_mem = state.index("deviceDispatch->vkFreeMemory(device, memory, nullptr);\n\n#ifdef __ANDROID__")
assert free_mem < release, "the driver must drop its import before the AHB reference goes"
assert "memoryInfo.ahbBacking = nullptr;" in state[release:release + 400]
assert "memoryInfo.ahbBackingBytes = 0;" in state[release:release + 400]
assert state.count("if (ahb) AHardwareBuffer_release(ahb);") == 1
assert "AHB-FIRST-FALLBACK" in state
fallback = state.index("AHB-FIRST-FALLBACK")
assert state.index("if (ahb) AHardwareBuffer_release(ahb);") < fallback

# 7. Untouched guarantees.
assert "rejectZeroSizeAllocation(localAllocInfo" in state, "zero-size guard must stay"
assert "validateHostPointerImport(" in state, "host-pointer contract helper must stay"
assert "info->poolOffset >= 0" in state and "STREAM_HANDLE_TYPE_MEM_POOL" in state
assert state.count("if (poolOffset < 0) {") == 1, "GpuPool branch keeps its shape"
assert "AHardwareBuffer_allocate(&desc, &ahb);\n    if (ahbRes != 0 || !ahb) {" in \
    (root / "host/vulkan/vk_common_operations.cpp").read_text(
        encoding="utf-8", errors="replace").replace("\r\n", "\n"), \
    "ColorBuffer AHB import must stay"

report = (
    "PASS MemoryInfo carries ahbBacking, stored with the host-pointer fields\n"
    "PASS AHB-first block gated on AndroidAHB+ExternalBlob, allocates before the memfd\n"
    "PASS memfd create, host-pointer append and SHARED_MEMORY publication all guarded\n"
    "PASS export returns the stored handle, refuses host-pointer imports, no relock\n"
    "PASS AHB released once after vkFreeMemory and cleared; failure path releases once\n"
    "PASS zero-size guard, host-pointer contract, GpuPool and ColorBuffer AHB paths intact\n"
)
print(report, end="")
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / "ahb-first-tests.txt").write_text(report)
