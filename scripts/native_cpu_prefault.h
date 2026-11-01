#pragma once
#include <atomic>
#include <cstdint>
#include <cstring>
namespace gfxstream { namespace host { namespace native_prefault {
inline int application(const char* env, const char* app) {
    if (!env || std::strcmp(env, "1") || !app) return -1;
    if (std::strcmp(app, "guest-native-cpu-crossview") == 0) return 0;
    if (std::strcmp(app, "guest-native-cpu-reverse") == 0) return 1;
    return -1;
}
struct Guard {
    uint64_t size;
    uint64_t page;
    bool owner;
    bool epochCurrent;
    bool mapped;
    bool physicalType8CoherentVisible;
};
inline bool valid(const Guard& g, const void* address) {
    const auto ptr = reinterpret_cast<uintptr_t>(address);
    return address && g.size == 4096 && g.page == 4096 && ptr % g.page == 0 &&
        ptr <= UINTPTR_MAX - 4095 && g.owner && g.epochCurrent && g.mapped &&
        g.physicalType8CoherentVisible;
}
class Budget {
public:
    bool consume(int app) {
        if (app < 0 || app > 1) return false;
        bool expected = false;
        return used[app].compare_exchange_strong(expected, true);
    }
private:
    std::atomic<bool> used[2]{};
};
inline Budget& processBudget() { static auto* budget = new Budget; return *budget; }
template<typename Begin>
inline bool touch(int app, const Guard& guard, const void* address, Budget& budget, uint32_t& initial, Begin beforeRead) {
    if (!valid(guard, address) || !budget.consume(app)) return false;
    beforeRead(); // observable BEGIN only after validation/budget, immediately before the read
    initial = *static_cast<const volatile uint32_t*>(address); // EXACTLY one read, no stores
#if defined(__aarch64__)
    __asm__ __volatile__("dsb sy" ::: "memory"); // ordering only, no cache maintenance
#else
    std::atomic_thread_fence(std::memory_order_seq_cst); // CPU-array CI fallback, not ARM evidence
#endif
    return true;
}
inline bool touch(int app, const Guard& guard, const void* address, Budget& budget, uint32_t& initial) {
    return touch(app, guard, address, budget, initial, [] {});
}
}}} // namespace gfxstream::host::native_prefault
