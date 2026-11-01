#include "native_cpu_crossview.h"
#include <array>
#include <cassert>
#include <initializer_list>
using namespace gfxstream::host::native_crossview;
int main() {
    for (const char* env : std::initializer_list<const char*>{nullptr, "", "0", "true", "01", "1 "})
        assert(!reverseEnabled(env, "guest-native-cpu-reverse"));
    for (const char* app : std::initializer_list<const char*>{nullptr, "", "guest-native-cpu-crossview", "guest-native-cpu-reverse-extra"})
        assert(!reverseEnabled("1", app));
    assert(reverseEnabled("1", "guest-native-cpu-reverse"));
    assert(!enabled("1", "guest-native-cpu-reverse"));
    Guard guard{4096, 0, 4096, true, true, true, true, false};
    std::array<uint32_t, 1024> words{}; words.fill(0x77777777);
    auto initial = words;
    Budget budget; Observation seen;
    assert(reverse(guard, words.data(), budget, seen) == Result::Replied);
    assert(seen.first == 0x77777777 && seen.last == 0x77777777); // forward mismatch recorded
    assert(words.front() == 0x89abcdef && words.back() == 0xfedcba98);
    for (unsigned i = 1; i < 1023; ++i) assert(words[i] == initial[i]);
    auto replied = words;
    assert(reverse(guard, words.data(), budget, seen) == Result::Exhausted);
    assert(words == replied);
    for (unsigned invalid = 0; invalid < 10; ++invalid) {
        auto bad = guard;
        if (invalid == 0) bad.size = 8192;
        if (invalid == 1) bad.offset = 4;
        if (invalid == 2) bad.rangeSize = UINT64_MAX;
        if (invalid == 3) bad.owned = false;
        if (invalid == 4) bad.epochCurrent = false;
        if (invalid == 5) bad.mapped = false;
        if (invalid == 6) bad.physicalCoherentVisible = false;
        if (invalid == 7) bad.bound = true;
        auto data = initial;
        void* pointer = data.data();
        if (invalid == 8) pointer = reinterpret_cast<uint8_t*>(data.data()) + 1;
        if (invalid == 9) pointer = reinterpret_cast<void*>(UINTPTR_MAX - 3);
        Budget invalidBudget;
        assert(reverse(bad, pointer, invalidBudget, seen) == Result::Invalid);
        assert(data == initial);
        assert(invalidBudget.consume());
    }
    // Forward rule is unchanged: same wrong requests MUST NOT write either reply.
    auto forward = initial; Budget forwardBudget;
    assert(exchange(guard, forward.data(), forwardBudget, seen) == Result::Mismatch);
    assert(forward == initial);
}
