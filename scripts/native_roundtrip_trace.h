#pragma once
#include <atomic>
#include <cstring>
namespace gfxstream { namespace host { namespace native_trace {
enum class Stage { Allocation, Mapping, Bind, Copy, CopyRegion, Submit, SubmitCommand, Wait,
                   FlushInline, FlushAux, Capability, Count };
inline bool enabled(const char* env, const char* application) {
    return env && std::strcmp(env, "1") == 0 && application &&
        (std::strcmp(application, "guest-native-buffer-roundtrip") == 0 ||
         std::strcmp(application, "guest-native-first-touch") == 0);
}
inline bool take(Stage stage, bool allowed) {
    if (!allowed) return false;
    static std::atomic<unsigned> counts[static_cast<unsigned>(Stage::Count)]{};
    auto& count = counts[static_cast<unsigned>(stage)];
    unsigned current = count.load(std::memory_order_relaxed);
    while (current < 16) {
        if (count.compare_exchange_weak(current, current + 1, std::memory_order_relaxed)) return true;
    }
    return false;
}
}}} // namespace gfxstream::host::native_trace
