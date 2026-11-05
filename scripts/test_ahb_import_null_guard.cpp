// Decision table for the default-on AndroidHardwareBuffer import guard.
//
// The production branch refuses a handle that cannot be an AHardwareBuffer* before it
// reaches VkImportAndroidHardwareBufferInfoANDROID::buffer, and records
// VK_ERROR_INVALID_EXTERNAL_HANDLE for the caller. This test pins that contract:
//
//   * 0 and fd-sized integers (1..4095) are rejected;
//   * a plausible address is stored unchanged, bit for bit;
//   * the log gate admits exactly one caller per process;
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

    puts("PASS: 0/fd handles rejected with VK_ERROR_INVALID_EXTERNAL_HANDLE, legal "
         "address stored unchanged, log gate single-winner and once per process");
    return 0;
}
