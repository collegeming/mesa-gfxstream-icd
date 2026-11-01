#pragma once
#include <atomic>
#include <cstdint>
#include <cstring>
namespace gfxstream { namespace host { namespace native_crossview {
inline bool enabled(const char* env, const char* app) {
    return env && std::strcmp(env, "1") == 0 && app &&
        std::strcmp(app, "guest-native-cpu-crossview") == 0;
}
struct Guard {
    uint64_t size;
    uint64_t offset;
    uint64_t rangeSize;
    bool owned;
    bool epochCurrent;
    bool mapped;
    bool physicalCoherentVisible;
    bool bound;
};
inline bool valid(const Guard& g, const void* pointer) {
    return pointer && reinterpret_cast<uintptr_t>(pointer) % alignof(uint32_t) == 0 &&
        reinterpret_cast<uintptr_t>(pointer) <= UINTPTR_MAX - 4095 &&
        g.size == 4096 && g.offset == 0 && g.rangeSize == 4096 && g.owned &&
        g.epochCurrent && g.mapped && g.physicalCoherentVisible && !g.bound;
}
class Budget {
public:
    bool consume() { bool expected = false; return used.compare_exchange_strong(expected, true); }
private:
    std::atomic<bool> used{false};
};
inline Budget& processBudget() { static auto* budget = new Budget; return *budget; }
enum class Result { Invalid, Exhausted, Mismatch, Replied };
struct Observation { uint32_t first = 0; uint32_t last = 0; };
inline Result exchange(const Guard& guard, void* pointer, Budget& budget, Observation& seen) {
    if (!valid(guard, pointer)) return Result::Invalid;
    if (!budget.consume()) return Result::Exhausted;
    // Ordering only, NOT cache maintenance. No atomic RMW on the PFN/IO mapping.
    std::atomic_thread_fence(std::memory_order_acquire);
    auto* bytes = static_cast<uint8_t*>(pointer);
    auto* first = reinterpret_cast<volatile uint32_t*>(bytes);
    auto* last = reinterpret_cast<volatile uint32_t*>(bytes + 4092);
    seen.first = *first;
    seen.last = *last;
    if (seen.first != 0x13579bdfu || seen.last != 0x2468ace0u) return Result::Mismatch;
    *first = 0x89abcdefu;
    *last = 0xfedcba98u;
    std::atomic_thread_fence(std::memory_order_release);
    return Result::Replied;
}
}}} // namespace gfxstream::host::native_crossview
