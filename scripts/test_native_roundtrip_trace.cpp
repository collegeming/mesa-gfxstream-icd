#include "native_roundtrip_trace.h"
#include <cassert>
#include <thread>
#include <vector>
using namespace gfxstream::host::native_trace;
int main() {
    for (const char* env : std::initializer_list<const char*>{nullptr, "", "0", "true", "01", "1 "})
        assert(!enabled(env, "guest-native-buffer-roundtrip"));
    for (const char* app : std::initializer_list<const char*>{nullptr, "", "niri", "guest-native-buffer-roundtrip-extra", "guest-native-first-touch-extra"})
        assert(!enabled("1", app));
    assert(enabled("1", "guest-native-buffer-roundtrip"));
    assert(enabled("1", "guest-native-first-touch"));
    for (unsigned i = 0; i < 100; ++i) assert(!take(Stage::Copy, false));
    std::atomic<unsigned> successes{0};
    std::vector<std::thread> threads;
    for (unsigned i = 0; i < 8; ++i) threads.emplace_back([&] {
        for (unsigned j = 0; j < 100; ++j) if (take(Stage::Copy, true)) successes.fetch_add(1);
    });
    for (auto& thread : threads) thread.join();
    assert(successes.load() == 16);
    assert(!take(Stage::Copy, true));
    for (unsigned i = 0; i < 16; ++i) assert(take(Stage::Bind, true));
    assert(!take(Stage::Bind, true));
}
