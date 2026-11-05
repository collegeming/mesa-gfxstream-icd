// Diagnostic-only keep-alive switch for ColorBuffer AHB teardown.
//
// This header is copied into host/vulkan/ahb_keep_alive.h by
// scripts/patch_ahb_keep_alive.py on the diagnostic/ahb-crash-stack branch.
//
// When the process environment contains exactly GFXSTREAM_AHB_KEEP_ALIVE=1,
// freeExternalMemoryLocked() skips the AHardwareBuffer_release() for the
// PLATFORM_AHB branch and writes a rate-limited AHB-KEEP-ALIVE line instead.
// The switch exists to test one hypothesis only: the ColorBuffer AHB is
// released while the driver still holds an import reference (use-after-free),
// so skipping that release should stop the crash. Every other release point and
// the whole import path stay unchanged.
//
// The environment is read on every call. There is deliberately no namespace
// scope or function-local static bool initialized from getenv(), so the
// default path cannot be frozen at library load.
#ifndef GFXSTREAM_HOST_VULKAN_AHB_KEEP_ALIVE_H_
#define GFXSTREAM_HOST_VULKAN_AHB_KEEP_ALIVE_H_

#if defined(__ANDROID__) || defined(GFXSTREAM_AHB_KEEP_ALIVE_HOST_TEST)

#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cstdint>

namespace gfxstream {
namespace ahb_keep_alive {

constexpr uint32_t kLogLimit = 16;

inline int KeepAliveTid() {
#if defined(__ANDROID__)
    return static_cast<int>(gettid());
#else
    return static_cast<int>(syscall(SYS_gettid));
#endif
}

// Reads GFXSTREAM_AHB_KEEP_ALIVE on every call; exact value "1" only.
inline bool Enabled() {
    const char* value = getenv("GFXSTREAM_AHB_KEEP_ALIVE");
    return value != nullptr && value[0] == '1' && value[1] == '\0';
}

// Logs the first kLogLimit skips and returns true for those calls; later skips
// are silent and return false. The counter is constant-initialized, never fed
// from getenv().
inline bool LogSkip(const void* pointer) {
    static std::atomic<uint32_t> logged{0};
    const uint32_t index = logged.fetch_add(1, std::memory_order_relaxed);
    if (index >= kLogLimit) return false;
    fprintf(stderr, "AHB-KEEP-ALIVE ptr=%p tid=%d\n", const_cast<void*>(pointer),
            KeepAliveTid());
    return true;
}

}  // namespace ahb_keep_alive
}  // namespace gfxstream

#endif  // __ANDROID__ || GFXSTREAM_AHB_KEEP_ALIVE_HOST_TEST

#endif  // GFXSTREAM_HOST_VULKAN_AHB_KEEP_ALIVE_H_
