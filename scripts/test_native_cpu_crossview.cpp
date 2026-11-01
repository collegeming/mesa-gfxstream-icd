#include "native_cpu_crossview.h"
#include <array>
#include <cassert>
#include <initializer_list>
#include <thread>
#include <vector>
using namespace gfxstream::host::native_crossview;
int main() {
    for (const char* env : std::initializer_list<const char*>{nullptr, "", "0", "true", "01", "1 "})
        assert(!enabled(env, "guest-native-cpu-crossview"));
    for (const char* app : std::initializer_list<const char*>{nullptr, "", "guest-native-buffer-roundtrip", "guest-native-cpu-crossview-extra"})
        assert(!enabled("1", app));
    assert(enabled("1", "guest-native-cpu-crossview"));
    Guard validGuard{4096, 0, 4096, true, true, true, true, false};
    std::array<uint32_t, 1024> good{};
    good.fill(0x55555555);
    good.front() = 0x13579bdf; good.back() = 0x2468ace0;
    auto before = good;
    Budget success;
    Observation seen;
    assert(exchange(validGuard, good.data(), success, seen) == Result::Replied);
    assert(seen.first == 0x13579bdf && seen.last == 0x2468ace0);
    assert(good.front() == 0x89abcdef && good.back() == 0xfedcba98);
    for (unsigned i = 1; i < 1023; ++i) assert(good[i] == before[i]);
    auto replied = good;
    assert(exchange(validGuard, good.data(), success, seen) == Result::Exhausted);
    assert(good == replied);
    for (unsigned endpoint = 0; endpoint != 2; ++endpoint) {
        auto mismatch = before;
        mismatch[endpoint ? 1023 : 0] ^= 1;
        auto original = mismatch;
        Budget budget;
        assert(exchange(validGuard, mismatch.data(), budget, seen) == Result::Mismatch);
        assert(mismatch == original); // neither word modified when either request differs
        assert(exchange(validGuard, mismatch.data(), budget, seen) == Result::Exhausted);
    }
    for (unsigned invalid = 0; invalid != 9; ++invalid) {
        auto guard = validGuard;
        if (invalid == 0) guard.size = 4095;
        if (invalid == 1) guard.offset = 4;
        if (invalid == 2) guard.rangeSize = UINT64_MAX;
        if (invalid == 3) guard.owned = false;
        if (invalid == 4) guard.epochCurrent = false;
        if (invalid == 5) guard.mapped = false;
        if (invalid == 6) guard.physicalCoherentVisible = false;
        if (invalid == 7) guard.bound = true;
        auto data = before;
        auto* pointer = invalid == 8 ? reinterpret_cast<uint8_t*>(data.data()) + 1 : reinterpret_cast<uint8_t*>(data.data());
        Budget budget;
        assert(exchange(guard, pointer, budget, seen) == Result::Invalid);
        assert(data == before);
        assert(exchange(validGuard, data.data(), budget, seen) == Result::Replied); // invalid did not consume
    }
    Budget nullBudget;
    assert(exchange(validGuard, nullptr, nullBudget, seen) == Result::Invalid);
    Budget concurrent;
    std::atomic<unsigned> winners{0};
    std::vector<std::thread> threads;
    // Separate arrays per thread: only budget is shared, not payload storage.
    for (unsigned i = 0; i < 8; ++i) threads.emplace_back([&] {
        auto data = before; Observation observation;
        if (exchange(validGuard, data.data(), concurrent, observation) == Result::Replied) ++winners;
    });
    for (auto& thread : threads) thread.join();
    assert(winners == 1);
}
