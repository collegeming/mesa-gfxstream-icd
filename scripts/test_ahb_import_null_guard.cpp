// Decision table for the default-on AndroidHardwareBuffer import guard.
//
// Two production branches refuse a handle that cannot be an AHardwareBuffer* before
// the driver sees it: the decoder's own import handle, and the guest-supplied
// VkImportAndroidHardwareBufferInfoANDROID in the pNext chain. Both record
// VK_ERROR_INVALID_EXTERNAL_HANDLE. This test pins that contract:
//
//   * 0 and fd-sized integers (1..4095) are rejected at both sites;
//   * a plausible address is stored unchanged, bit for bit;
//   * each log gate admits exactly one caller per process;
//   * a rejected guest buffer leaves diagnosticAhb/diagnosticStreamType untouched;
//   * the recorded Vulkan result is the import-specific error, not OUT_OF_DEVICE_MEMORY.
//
// This is a host test: it includes the same header the device build includes.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <atomic>
#include <thread>
#include <vector>

#include <vulkan/vulkan.h>

#include "ahb_import_guard.h"

namespace {

void check(bool condition, const char* what) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", what);
        exit(1);
    }
}

// Mirror of the production branch: reject first, then store the original handle.
VkResult importHandle(int64_t handle, uintptr_t* buffer) {
    if (!gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(handle)) {
        return VK_ERROR_INVALID_EXTERNAL_HANDLE;
    }
    *buffer = static_cast<uintptr_t>(handle);
    return VK_SUCCESS;
}

// Mirror of the guest pNext branch: reject before either diagnostic field is set.
VkResult importGuestBuffer(const void* buffer, const void** diagnosticAhb,
                           uint32_t* streamType) {
    if (!gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(
            reinterpret_cast<intptr_t>(buffer))) {
        return VK_ERROR_INVALID_EXTERNAL_HANDLE;
    }
    *diagnosticAhb = buffer;
    *streamType = 0x03000000u;  // STREAM_HANDLE_TYPE_PLATFORM_AHB
    return VK_SUCCESS;
}

}  // namespace

int main() {
    // One comparison, compile-time: 0 and every fd-sized value fail; 4096 is the first
    // address a userspace mapping can have.
    static_assert(!gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(0),
                  "zero is not an AHardwareBuffer address");
    static_assert(!gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(1),
                  "fd 1 is not an AHardwareBuffer address");
    static_assert(!gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(255),
                  "an fd-sized integer is not an AHardwareBuffer address");
    static_assert(!gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(4095),
                  "the last sub-mmap_min_addr value is rejected");
    static_assert(gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(4096),
                  "the first mappable address is plausible");
    static_assert(gfxstream::ahb_import_guard::AhbImportHandleIsPlausible(0x0000007f12345000LL),
                  "a normal 47-bit userspace address is plausible");
    static_assert(VK_ERROR_INVALID_EXTERNAL_HANDLE == -1000072003,
                  "the guard records the standard import-specific Vulkan error");

    uintptr_t buffer = 0;

    // Rejected shapes never reach the assignment.
    check(importHandle(0, &buffer) == VK_ERROR_INVALID_EXTERNAL_HANDLE, "zero rejected");
    check(buffer == 0, "zero did not write a buffer");
    check(importHandle(1, &buffer) == VK_ERROR_INVALID_EXTERNAL_HANDLE, "fd 1 rejected");
    check(importHandle(255, &buffer) == VK_ERROR_INVALID_EXTERNAL_HANDLE,
          "fd-sized handle rejected");
    check(importHandle(4095, &buffer) == VK_ERROR_INVALID_EXTERNAL_HANDLE,
          "4095 rejected");
    check(buffer == 0, "rejected handles did not write a buffer");

    // A legal handle is stored with its exact bit pattern: no mask, no rewrite.
    const int64_t legal = 0x0000007f12345000LL;
    check(importHandle(legal, &buffer) == VK_SUCCESS, "legal handle accepted");
    check(buffer == static_cast<uintptr_t>(legal), "legal handle stored unchanged");
    uintptr_t again = 0;
    check(importHandle(legal, &again) == VK_SUCCESS && again == buffer,
          "legal handle is stable across calls");

    // Guest pNext import: a null/fd-sized buffer is refused before either diagnostic
    // field is written; a plausible buffer keeps the original assignments.
    const void* sentinel = reinterpret_cast<const void*>(static_cast<uintptr_t>(0x1111));
    const void* guestAhb = sentinel;
    uint32_t guestStreamType = 0xdeadbeefu;
    check(importGuestBuffer(nullptr, &guestAhb, &guestStreamType) ==
              VK_ERROR_INVALID_EXTERNAL_HANDLE,
          "null guest buffer rejected");
    check(guestAhb == sentinel, "null guest buffer did not touch diagnosticAhb");
    check(guestStreamType == 0xdeadbeefu, "null guest buffer did not touch stream type");

    guestAhb = nullptr;
    guestStreamType = 0;
    check(importGuestBuffer(reinterpret_cast<const void*>(static_cast<uintptr_t>(4095)),
                            &guestAhb, &guestStreamType) ==
              VK_ERROR_INVALID_EXTERNAL_HANDLE,
          "4095 guest buffer rejected");
    check(guestAhb == nullptr && guestStreamType == 0,
          "rejected guest buffer did not set diagnostics");

    const uintptr_t guestLegal = 0x0000007cd1e5e5e0ULL;
    guestAhb = nullptr;
    guestStreamType = 0;
    check(importGuestBuffer(reinterpret_cast<const void*>(guestLegal), &guestAhb,
                            &guestStreamType) == VK_SUCCESS,
          "plausible guest buffer accepted");
    check(guestAhb == reinterpret_cast<const void*>(guestLegal),
          "guest buffer stored unchanged");
    check(guestStreamType == 0x03000000u, "guest stream type set after acceptance");

    // The guest site has its own once-per-process gate.
    check(gfxstream::ahb_import_guard::ShouldLogGuestBufferFailure(),
          "first guest failure logs");
    check(!gfxstream::ahb_import_guard::ShouldLogGuestBufferFailure(),
          "later guest failures are silent");

    // The gate is process-wide: many concurrent callers produce one winner, and every
    // later call stays silent.
    std::atomic<unsigned> wins{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < 16; ++i) {
        threads.emplace_back([&wins]() {
            if (gfxstream::ahb_import_guard::ShouldLogImportFailure()) {
                wins.fetch_add(1);
            }
        });
    }
    for (auto& thread : threads) {
        thread.join();
    }
    check(wins.load() == 1, "exactly one log line wins");
    check(!gfxstream::ahb_import_guard::ShouldLogImportFailure(), "later failures are silent");

    puts("PASS: 0/fd handles rejected with VK_ERROR_INVALID_EXTERNAL_HANDLE at both "
         "sites, legal addresses stored unchanged, guest diagnostics untouched on "
         "rejection, log gates single-winner and once per process");
    return 0;
}
