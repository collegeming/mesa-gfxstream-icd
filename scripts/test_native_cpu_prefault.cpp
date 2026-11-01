#include "native_cpu_prefault.h"
#include <array>
#include <cassert>
#include <initializer_list>
using namespace gfxstream::host::native_prefault;
int main() {
    assert(application("1", "guest-native-cpu-crossview") == 0);
    assert(application("1", "guest-native-cpu-reverse") == 1);
    for (const char* env : std::initializer_list<const char*>{nullptr, "", "0", "true", "01", "1 "})
        assert(application(env, "guest-native-cpu-crossview") == -1);
    assert(application("1", "guest-native-buffer-roundtrip") == -1);
    assert(application("1", "guest-native-cpu-crossview-extra") == -1);
    alignas(4096) std::array<uint32_t, 1024> data{};
    data.fill(0x12345678); auto before = data;
    Guard good{4096, 4096, true, true, true, true};
    Budget budget; uint32_t observed = 0;
    unsigned begins = 0;
    assert(touch(0, good, data.data(), budget, observed, [&] { ++begins; assert(observed == 0); }));
    assert(begins == 1);
    assert(observed == 0x12345678 && data == before);
    assert(!touch(0, good, data.data(), budget, observed, [&] { ++begins; }));
    assert(begins == 1); // rejected budget cannot emit BEGIN or read
    assert(touch(1, good, data.data(), budget, observed));
    assert(!touch(1, good, data.data(), budget, observed));
    assert(data == before);
    for (unsigned i = 0; i < 9; ++i) {
        auto invalid = good;
        if (i == 0) invalid.size = 8192;
        if (i == 1) invalid.page = 0;
        if (i == 2) invalid.page = 65536;
        if (i == 3) invalid.owner = false;
        if (i == 4) invalid.epochCurrent = false;
        if (i == 5) invalid.mapped = false;
        if (i == 6) invalid.physicalType8CoherentVisible = false;
        const void* ptr = data.data();
        if (i == 7) ptr = reinterpret_cast<const uint8_t*>(data.data()) + 4;
        if (i == 8) ptr = nullptr;
        Budget rejected;
        assert(!touch(0, invalid, ptr, rejected, observed, [&] { ++begins; }));
        assert(begins == 1); // invalid guard cannot emit BEGIN or read
        assert(touch(0, good, data.data(), rejected, observed));
        assert(data == before);
    }
}
