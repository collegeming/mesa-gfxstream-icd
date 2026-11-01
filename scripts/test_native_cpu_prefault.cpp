#include "native_cpu_prefault.h"
#include <array>
#include <cassert>
#include <initializer_list>
using namespace gfxstream::host::native_prewrite;
int main() {
    assert(application("1", "guest-native-cpu-crossview") == 0);
    assert(application("1", "guest-native-cpu-reverse") == 1);
    for (const char* env : std::initializer_list<const char*>{nullptr, "", "0", "true", "01", "1 "})
        assert(application(env, "guest-native-cpu-crossview") == -1);
    assert(application("1", "guest-native-buffer-roundtrip") == -1);
    assert(application("1", "guest-native-cpu-crossview-extra") == -1);
    alignas(4096) std::array<uint32_t, 1024> data{};
    data.fill(0x12345678); auto before = data;
    Guard good{4096, 4096, true, true, true, true, true};
    Budget budget;
    unsigned begins = 0;
    assert(touch(0, good, data.data(), budget, [&] { ++begins; assert(data == before); }));
    assert(begins == 1 && data.front() == 0x13579bdf);
    for (unsigned i = 1; i < data.size(); ++i) assert(data[i] == before[i]);
    auto written = data;
    assert(!touch(0, good, data.data(), budget, [&] { ++begins; }));
    assert(begins == 1 && data == written);
    assert(touch(1, good, data.data(), budget));
    assert(!touch(1, good, data.data(), budget));
    assert(data == written);
    for (unsigned i = 0; i < 10; ++i) {
        auto invalid = good;
        if (i == 0) invalid.size = 8192;
        if (i == 1) invalid.page = 0;
        if (i == 2) invalid.page = 65536;
        if (i == 3) invalid.owner = false;
        if (i == 4) invalid.epochCurrent = false;
        if (i == 5) invalid.mapped = false;
        if (i == 6) invalid.physicalType8CoherentVisible = false;
        if (i == 9) invalid.freshUnbound = false;
        void* ptr = data.data();
        if (i == 7) ptr = reinterpret_cast<uint8_t*>(data.data()) + 4;
        if (i == 8) ptr = nullptr;
        Budget rejected;
        assert(!touch(0, invalid, ptr, rejected, [&] { ++begins; }));
        assert(begins == 1 && data == written);
        assert(touch(0, good, data.data(), rejected));
    }
}
