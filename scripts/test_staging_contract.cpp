#include "staging_contract.h"
#include <array>
#include <cstdlib>
#include <iostream>
#include <limits>
using namespace gfxstream::host::vk::staging;
namespace {
int checks = 0;
void check(bool pass, int line) {
    ++checks;
    if (!pass) { std::cerr << "FAIL line " << line << '\n'; std::abort(); }
}
#define CHECK(...) check((__VA_ARGS__), __LINE__)
struct Mock {
    // Deliberately separate buffers. No Vulkan loader or GPU is used.
    std::array<unsigned char, 130> cpu{}, gpu{};
    VkResult uploadResult = VK_SUCCESS, submitResult = VK_SUCCESS;
    VkResult completionResult = VK_SUCCESS, downloadResult = VK_SUCCESS;
    VkResult closeResult = VK_SUCCESS, releaseResult = VK_SUCCESS;
    bool signaled = true, wrongEpoch = false, shortCoverage = false, overflowCoverage = false;
    int uploads = 0, submits = 0, polls = 0, downloads = 0, closes = 0, releases = 0;
    static VkResult upload(void* p, uint64_t, Range r) {
        auto& m = *static_cast<Mock*>(p); ++m.uploads;
        if (m.uploadResult != VK_SUCCESS) return m.uploadResult;
        for (VkDeviceSize i = r.offset; i < r.offset + r.size; ++i) m.gpu[i] = m.cpu[i];
        return VK_SUCCESS;
    }
    static VkResult submit(void* p, uint64_t, Range r) {
        auto& m = *static_cast<Mock*>(p); ++m.submits;
        if (m.submitResult != VK_SUCCESS) return m.submitResult;
        for (VkDeviceSize i = r.offset; i < r.offset + r.size; ++i) m.gpu[i] ^= 0x5a;
        return VK_SUCCESS; // Mock GPU only, not real backend integration.
    }
    static VkResult complete(void* p, uint64_t epoch, Range r, Completion* proof) {
        auto& m = *static_cast<Mock*>(p); ++m.polls;
        *proof = {m.wrongEpoch ? epoch + 1 : epoch, r, m.signaled};
        if (m.shortCoverage) proof->coverage.size = r.size - 1;
        if (m.overflowCoverage) proof->coverage = {129, VK_WHOLE_SIZE};
        return m.completionResult;
    }
    static VkResult download(void* p, uint64_t, Range r) {
        auto& m = *static_cast<Mock*>(p); ++m.downloads;
        if (m.downloadResult != VK_SUCCESS) {
            m.cpu[r.offset] = 0xee; // Even a partial copy must not grant readability.
            return m.downloadResult;
        }
        for (VkDeviceSize i = r.offset; i < r.offset + r.size; ++i) m.cpu[i] = m.gpu[i];
        return VK_SUCCESS;
    }
    static VkResult close(void* p, int fd) {
        auto& m = *static_cast<Mock*>(p); CHECK(fd == 77); ++m.closes; return m.closeResult;
    }
    static VkResult release(void* p, uint64_t offset, uint64_t size) {
        auto& m = *static_cast<Mock*>(p); CHECK(offset == 256 && size == 130);
        ++m.releases; return m.releaseResult;
    }
    Ownership ownership() { return {this, 77, true, 256, 130, close, release}; }
    Readiness ready() {
        Readiness r;
        r.hostProtocol = r.guestProtocol = 1; r.hostPair = r.guestPair = 9;
        r.coverage = AllCoverage;
        r.staging = r.expectedStaging = {1, 2, 130, 0, 130};
        r.gpu = r.expectedGpu = {3, 4, 130, 0, 130};
        r.atom = 64; r.maximumRange = 130;
        r.callbacks = {this, upload, submit, complete, download};
        return r;
    }
};
void ranges() {
    Range r{999, 999};
    CHECK(normalizeRange(130, 64, 130, 65, VK_WHOLE_SIZE, &r) == VK_SUCCESS);
    CHECK(r.offset == 64 && r.size == 66); // Actual allocation tail.
    CHECK(normalizeRange(130, 64, 130, 129, 1, &r) == VK_SUCCESS);
    CHECK(r.offset == 128 && r.size == 2);
    CHECK(normalizeRange(130, 64, 63, 1, 1, &r) != VK_SUCCESS);
    CHECK(normalizeRange(130, 64, 130, 130, VK_WHOLE_SIZE, &r) != VK_SUCCESS);
    CHECK(normalizeRange(130, 64, 130, 1, 0, &r) != VK_SUCCESS);
    CHECK(normalizeRange(130, 3, 130, 1, 1, &r) != VK_SUCCESS);
    CHECK(normalizeRange(130, 0, 130, 1, 1, &r) != VK_SUCCESS);
    CHECK(normalizeRange(0, 64, 130, 0, 1, &r) != VK_SUCCESS);
    CHECK(normalizeRange(VK_WHOLE_SIZE, 64, 130, 0, 1, &r) != VK_SUCCESS);
    CHECK(normalizeRange(130, 64, 130, 129, VK_WHOLE_SIZE - 1, &r) != VK_SUCCESS);
    const auto max = std::numeric_limits<VkDeviceSize>::max() - 1;
    CHECK(normalizeRange(max, 64, max, max - 2, 2, &r) == VK_SUCCESS);
    CHECK(r.offset + r.size == max && r.size == 62);
    CHECK(normalizeRange(max, 64, max, max - 2, 3, &r) != VK_SUCCESS);
    CHECK(normalizeRange(130, 64, 130, 0, 1, nullptr) != VK_SUCCESS);
    CHECK(!contains({0, 130}, {129, VK_WHOLE_SIZE}));
}
void readiness() {
    Mock m; auto r = m.ready();
    CHECK(checkReadiness(r) == VK_SUCCESS);
    for (int i = 0; i < 16; ++i) {
        auto bad = r;
        switch (i) {
            case 0: bad.guestProtocol = 0; break;
            case 1: bad.guestPair++; break;
            case 2: bad.directMapping = true; break;
            case 3: bad.coverage &= ~FencePollAndWait; break;
            case 4: bad.coverage &= ~QueueIdle; break;
            case 5: bad.coverage &= ~DeviceIdle; break;
            case 6: bad.staging.generation++; break;
            case 7: bad.gpu.object++; break;
            case 8: bad.callbacks.uploadAndFlush = nullptr; break;
            case 9: bad.callbacks.submitGpu = nullptr; break;
            case 10: bad.callbacks.completion = nullptr; break;
            case 11: bad.callbacks.downloadAndInvalidate = nullptr; break;
            case 12: bad.staging.mappedSize = VK_WHOLE_SIZE; break;
            case 13: bad.atom = 3; break;
            case 14: bad.maximumRange = 0; break;
            case 15: bad.gpu = bad.expectedGpu = bad.staging; break;
        }
        const char* why = nullptr;
        CHECK(checkReadiness(bad, &why) == VK_ERROR_FEATURE_NOT_PRESENT && why);
        Contract c(bad, m.ownership());
        CHECK(c.guestWrite(bad.staging, 0, 1) != VK_SUCCESS);
        CHECK(c.retire() == VK_SUCCESS && c.retire() == VK_SUCCESS);
    }
    CHECK(m.closes == 16 && m.releases == 16 && m.uploads == 0);
    Readiness unavailable{}; // Exactly what production currently passes.
    CHECK(allocationGate(false, true, true, unavailable) == VK_SUCCESS);
    CHECK(allocationGate(true, false, true, unavailable) == VK_SUCCESS);
    CHECK(allocationGate(true, true, false, unavailable) == VK_SUCCESS);
    CHECK(allocationGate(true, true, true, unavailable) == VK_ERROR_FEATURE_NOT_PRESENT);
    CHECK(allocationGate(true, true, true, r) == VK_SUCCESS); // Only mock attestation.
}
void visibility() {
    Mock m; auto r = m.ready(); Contract c(r, m.ownership());
    CHECK(!c.guestReadable(0));
    auto wrong = r.staging; ++wrong.generation;
    CHECK(c.guestWrite(wrong, 0, 1) != VK_SUCCESS);
    CHECK(c.guestWrite(r.staging, 65, VK_WHOLE_SIZE) == VK_SUCCESS);
    const auto epoch = c.epoch();
    m.cpu[65] = 0x21;
    CHECK(m.gpu[65] == 0 && !c.guestReadable(epoch));
    CHECK(c.submit(epoch) != VK_SUCCESS);
    CHECK(c.upload(epoch + 1) != VK_SUCCESS && m.uploads == 0);
    CHECK(c.upload(epoch) == VK_SUCCESS && m.gpu[65] == 0x21);
    CHECK(c.submit(epoch) == VK_SUCCESS && m.gpu[65] == (0x21 ^ 0x5a));
    CHECK(m.cpu[65] == 0x21 && !c.guestReadable(epoch));
    CHECK(c.retire() == VK_NOT_READY && m.closes == 0 && m.releases == 0);
    CHECK(c.guestWrite(r.staging, 0, 1) != VK_SUCCESS);
    CHECK(c.download(epoch) != VK_SUCCESS && m.downloads == 0);
    CHECK(c.complete(epoch + 1) != VK_SUCCESS && m.polls == 0);
    for (int i = 0; i < 6; ++i) {
        m.completionResult = i == 0 ? VK_NOT_READY : i == 1 ? VK_TIMEOUT : VK_SUCCESS;
        m.signaled = i != 2; m.wrongEpoch = i == 3;
        m.shortCoverage = i == 4; m.overflowCoverage = i == 5;
        CHECK(c.complete(epoch) != VK_SUCCESS && c.inFlight());
        CHECK(c.retire() == VK_NOT_READY && !c.guestReadable(epoch));
    }
    m.overflowCoverage = false;
    CHECK(c.complete(epoch) == VK_SUCCESS && !c.inFlight());
    CHECK(!c.guestReadable(epoch) && m.cpu[65] == 0x21);
    CHECK(c.download(epoch) == VK_SUCCESS && m.cpu[65] == (0x21 ^ 0x5a));
    CHECK(c.guestReadable(epoch));
    CHECK(c.guestWrite(r.staging, 0, 1) == VK_SUCCESS && c.epoch() == epoch + 1);
    CHECK(!c.guestReadable(epoch) && c.upload(epoch) != VK_SUCCESS);
    CHECK(c.retire() == VK_SUCCESS && c.retire() == VK_SUCCESS);
    CHECK(m.closes == 1 && m.releases == 1);
}
void failures() {
    for (int stage = 0; stage < 4; ++stage) {
        Mock m; auto r = m.ready();
        Contract c(r, m.ownership());
        CHECK(c.guestWrite(r.staging, 0, 1) == VK_SUCCESS);
        auto e = c.epoch(); m.cpu[0] = 7;
        if (stage == 0) m.uploadResult = VK_ERROR_MEMORY_MAP_FAILED;
        CHECK(c.upload(e) == (stage == 0 ? VK_ERROR_MEMORY_MAP_FAILED : VK_SUCCESS));
        if (stage > 0) {
            if (stage == 1) m.submitResult = VK_ERROR_DEVICE_LOST;
            CHECK(c.submit(e) == (stage == 1 ? VK_ERROR_DEVICE_LOST : VK_SUCCESS));
            if (stage == 2) m.completionResult = VK_ERROR_DEVICE_LOST;
            if (stage == 1 || stage == 2) {
                if (stage == 2) CHECK(c.complete(e) == VK_ERROR_DEVICE_LOST);
                CHECK(c.inFlight() && c.retire() == VK_NOT_READY);
                CHECK(c.download(e) != VK_SUCCESS && !c.guestReadable(e));
                m.completionResult = VK_SUCCESS;
                CHECK(c.complete(e) == VK_ERROR_INITIALIZATION_FAILED && !c.inFlight());
            } else {
                CHECK(c.complete(e) == VK_SUCCESS);
                m.downloadResult = VK_ERROR_MEMORY_MAP_FAILED;
                CHECK(c.download(e) == VK_ERROR_MEMORY_MAP_FAILED && m.cpu[0] == 0xee);
            }
        }
        CHECK(c.state() == Contract::State::Failed && !c.guestReadable(e));
        CHECK(c.guestWrite(r.staging, 0, 1) != VK_SUCCESS);
        CHECK(c.download(e) != VK_SUCCESS);
        CHECK(c.retire() == VK_SUCCESS && c.retire() == VK_SUCCESS);
        CHECK(m.closes == 1 && m.releases == 1);
    }
    Mock m; m.closeResult = VK_ERROR_UNKNOWN; m.releaseResult = VK_ERROR_DEVICE_LOST;
    { Contract c(m.ready(), m.ownership());
      CHECK(c.retire() == VK_ERROR_UNKNOWN && c.retire() == VK_ERROR_UNKNOWN); }
    CHECK(m.closes == 1 && m.releases == 1); // Both cleanups even if close fails.
    Mock automatic;
    { Contract c(automatic.ready(), automatic.ownership()); }
    CHECK(automatic.closes == 1 && automatic.releases == 1);
    Mock partial; auto r = partial.ready();
    r.staging = r.expectedStaging = {1, 2, 130, 1, 129};
    Contract c(r);
    CHECK(c.guestWrite(r.staging, 1, 1) != VK_SUCCESS); // Atom expansion escapes mapping.
    r = partial.ready(); r.gpu = r.expectedGpu = {3, 4, 130, 0, 129};
    Contract tail(r);
    CHECK(tail.guestWrite(r.staging, 128, 1) != VK_SUCCESS); // Map tail != allocation tail.
}
} // namespace
int main(int argc, char**) {
    if (argc > 1) {
        // Separate CI process proves the C++ destructor cannot free in-flight work.
        std::set_terminate([] { std::_Exit(0); });
        {
            Mock m; auto r = m.ready(); Contract c(r, m.ownership());
            CHECK(c.guestWrite(r.staging, 0, 1) == VK_SUCCESS);
            CHECK(c.upload(c.epoch()) == VK_SUCCESS);
            CHECK(c.submit(c.epoch()) == VK_SUCCESS);
        }
        return 1; // Destructor must have terminated instead of silently freeing.
    }
    ranges(); readiness(); visibility(); failures();
    std::cout << "PASS: " << checks << " staging contract checks; separate CPU/GPU mock buffers; "
                 "no GPU, Vulkan loader, real fence or backend callback integration\n";
}
