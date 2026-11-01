#pragma once
#include <atomic>
#include <cstdint>
#include <cstring>
namespace gfxstream { namespace host { namespace native_prewrite {
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
    bool freshUnbound;
};
inline bool valid(const Guard& g, const void* address) {
    const auto ptr = reinterpret_cast<uintptr_t>(address);
    return address && g.size == 4096 && g.page == 4096 && ptr % g.page == 0 &&
        ptr <= UINTPTR_MAX - 4095 && g.owner && g.epochCurrent && g.mapped &&
        g.physicalType8CoherentVisible && g.freshUnbound;
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
inline bool touch(int app, const Guard& guard, void* address, Budget& budget, Begin beforeStore) {
    if (!valid(guard, address) || !budget.consume(app)) return false;
    beforeStore(); // BEGIN after validation/budget, immediately before the store
    *static_cast<volatile uint32_t*>(address) = 0x13579bdfu; // ONE store; NO payload reads
#if defined(__aarch64__)
    __asm__ __volatile__("dsb sy" ::: "memory"); // ordering only, no cache maintenance
#else
    std::atomic_thread_fence(std::memory_order_seq_cst); // CPU-array CI fallback
#endif
    return true;
}
inline bool touch(int app, const Guard& guard, void* address, Budget& budget) {
    return touch(app, guard, address, budget, [] {});
}
}}} // namespace gfxstream::host::native_prewrite
