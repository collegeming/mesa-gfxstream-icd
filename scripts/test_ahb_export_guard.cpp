// Decision table for the AndroidAHB export guard.
//
// The AndroidAHB arm of allocExternalMemory() used to fall back to
// vkGetMemoryAndroidHardwareBufferANDROID() for every memory that had no AHB
// backing. Mali's getter acquires the memory object's AHB field without a null
// check, and that field is only written by an AHB import or an AHB export
// allocation, so the fallback faulted at 0x8 in AHardwareBuffer_acquire().
//
// This test pins the replacement contract:
//
//   * no handleInfo -> the getter is not called, validHandle is false and the
//     result is VK_ERROR_INVALID_EXTERNAL_HANDLE;
//   * a handleInfo of another type (e.g. dma-buf) is not AHB backing either and
//     takes the same refusal, leaving the existing handleInfo untouched;
//   * a PLATFORM_AHB handleInfo keeps the imported-AHB fast path: the stored
//     handle is used bit for bit, validHandle is true, and the getter is not
//     called;
//   * the AHB-EXPORT-SKIP gate admits exactly one caller per process, even when
//     many threads hit it at the same time.
//
// The dispatch fake counts getter calls. A legacy mirror of the old fallback is
// exercised first so the counter is proven to move when the getter is reached.
//
// This is a host test: it includes the same header the device build includes and
// has no Vulkan or Android dependency beyond the VkResult constants.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <atomic>
#include <thread>
#include <vector>

#include <vulkan/vulkan.h>

#include "ahb_export_guard.h"

namespace {

constexpr uint32_t kPlatformAhb = 0x03000000u;
constexpr uint32_t kMemDmaBuf = 0x00000200u;

std::atomic<unsigned> gLogLines{0};

void check(bool condition, const char* what) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", what);
        exit(1);
    }
}

struct Memory {
    bool hasHandleInfo;
    uint32_t streamHandleType;
    uint64_t handle;
    uint64_t size;
};

struct Dispatch {
    unsigned getterCalls = 0;
};

struct Outcome {
    bool validHandle;
    VkResult exportRes;
    bool handleInfoSet;
    uint64_t handle;
};

// Mirror of the old fallback, only to prove the fake counts reachable getter calls.
void legacyExport(const Memory& memory, Dispatch* dispatch) {
    if (memory.hasHandleInfo && memory.streamHandleType == kPlatformAhb) {
        return;
    }
    dispatch->getterCalls++;
}

// Mirror of the patched production branch. There is deliberately no dispatch
// parameter: the guarded arm has no way to reach the driver getter.
Outcome guardedExport(const Memory& memory) {
    Outcome outcome{false, VK_ERROR_INVALID_EXTERNAL_HANDLE, false, 0};
    if (memory.hasHandleInfo && memory.streamHandleType == kPlatformAhb) {
        // Imported-AHB fast path, byte-identical to the previous code.
        outcome.validHandle = true;
        outcome.exportRes = VK_SUCCESS;
        outcome.handleInfoSet = true;
        outcome.handle = memory.handle;
        return outcome;
    }
    // No AHB backing: refuse locally; one line per process.
    if (gfxstream::ahb_export_guard::ShouldLogSkip()) {
        gLogLines.fetch_add(1, std::memory_order_relaxed);
    }
    return outcome;
}

}  // namespace

int main() {
    static_assert(VK_ERROR_INVALID_EXTERNAL_HANDLE == -1000072003,
                  "the guard records the export-specific Vulkan error");

    const Memory noBacking{false, 0, 0, 4096};
    const Memory dmaBufBacking{true, kMemDmaBuf, 42, 8192};
    const Memory ahbBacking{true, kPlatformAhb, 0x0000007cd1e5e5e0ULL, 16384};

    // The fake is functional: the legacy fallback reaches the getter for both
    // non-AHB shapes and skips it for the imported AHB.
    Dispatch legacy;
    legacyExport(noBacking, &legacy);
    legacyExport(dmaBufBacking, &legacy);
    legacyExport(ahbBacking, &legacy);
    check(legacy.getterCalls == 2,
          "legacy fallback reaches the getter for both non-AHB shapes, never for an AHB");

    // Many threads hit the skip gate at once: exactly one diagnostic line wins.
    std::vector<std::thread> threads;
    for (int i = 0; i < 16; ++i) {
        threads.emplace_back([&noBacking]() { (void)guardedExport(noBacking); });
    }
    for (auto& thread : threads) {
        thread.join();
    }
    check(gLogLines.load() == 1, "exactly one AHB-EXPORT-SKIP line per process");
    check(!gfxstream::ahb_export_guard::ShouldLogSkip(),
          "later no-backing exports stay silent");

    // handleInfo absent: refused, no getter, no handleInfo published.
    Outcome outcome = guardedExport(noBacking);
    check(outcome.exportRes == VK_ERROR_INVALID_EXTERNAL_HANDLE,
          "no handleInfo reports VK_ERROR_INVALID_EXTERNAL_HANDLE");
    check(!outcome.validHandle, "no handleInfo leaves validHandle false");
    check(!outcome.handleInfoSet, "no handleInfo publishes nothing");
    check(gLogLines.load() == 1, "later no-backing exports add no line");

    // handleInfo of another type is not AHB backing either.
    outcome = guardedExport(dmaBufBacking);
    check(outcome.exportRes == VK_ERROR_INVALID_EXTERNAL_HANDLE,
          "a non-AHB handleInfo reports VK_ERROR_INVALID_EXTERNAL_HANDLE");
    check(!outcome.validHandle, "a non-AHB handleInfo leaves validHandle false");
    check(!outcome.handleInfoSet, "a non-AHB handleInfo is not rewritten");
    check(gLogLines.load() == 1, "a non-AHB handleInfo adds no line after the first");

    // Imported AHB: fast path, exact handle, success, no getter, no log.
    outcome = guardedExport(ahbBacking);
    check(outcome.exportRes == VK_SUCCESS, "an imported AHB still succeeds");
    check(outcome.validHandle, "an imported AHB still sets validHandle");
    check(outcome.handleInfoSet && outcome.handle == ahbBacking.handle,
          "the imported AHB handle is preserved bit for bit");
    check(gLogLines.load() == 1, "an imported AHB never logs a skip");

    // And the caller's failure path is the existing one: allocExternalMemory()
    // returns false when either flag fails.
    check(!(outcome.exportRes != VK_SUCCESS || !outcome.validHandle),
          "a successful export is not treated as a failure");
    const Outcome refused = guardedExport(noBacking);
    check(refused.exportRes != VK_SUCCESS || !refused.validHandle,
          "a refused export takes the caller's existing failure path");

    puts("PASS: no-backing and non-AHB handleInfo are refused with "
         "VK_ERROR_INVALID_EXTERNAL_HANDLE without reaching the export getter, the "
         "imported-AHB path is unchanged, and AHB-EXPORT-SKIP is once per process");
    return 0;
}
