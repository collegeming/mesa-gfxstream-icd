// Host-side unit test for the diagnostic AHB lifecycle tracer.
//
// Build (CI does this):
//   c++ -std=c++17 -pthread -Wall -Wextra -Werror -fno-omit-frame-pointer
//       -I source/host/vulkan scripts/test_ahb_crash_stack.cpp -o test-ahb-crash-stack
//
// The test proves the properties that make the tracer safe to ship in a
// diagnostic build: it is fully off unless GFXSTREAM_CRASH_TRACE=1, it installs
// only once, each crumb carries its writer's tid, and the ring holds at least
// 64 entries so a whole AHB lifecycle survives to the crash dump.

#define GFXSTREAM_AHB_CRASH_TRACE_HOST_TEST 1
#include "ahb_crash_trace.h"

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace trace = gfxstream::ahb_crash_trace;

static int gFailures = 0;

static void expect(bool condition, const char* what) {
    if (condition) {
        std::printf("PASS: %s\n", what);
    } else {
        std::printf("FAIL: %s\n", what);
        ++gFailures;
    }
}

static std::string readFile(const char* path) {
    std::string out;
    FILE* f = std::fopen(path, "rb");
    if (f == nullptr) return out;
    char buf[1024];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    std::fclose(f);
    return out;
}

static size_t countOf(const std::string& text, const std::string& needle) {
    size_t count = 0;
    for (size_t pos = text.find(needle); pos != std::string::npos;
         pos = text.find(needle, pos + needle.size())) {
        ++count;
    }
    return count;
}

// First decimal integer following `key` in `line`.
static long long numberAfter(const std::string& line, const std::string& key) {
    size_t pos = line.find(key);
    if (pos == std::string::npos) return -1;
    pos += key.size();
    long long value = 0;
    bool any = false;
    while (pos < line.size() && line[pos] >= '0' && line[pos] <= '9') {
        value = value * 10 + (line[pos] - '0');
        any = true;
        ++pos;
    }
    return any ? value : -1;
}

int main() {
    // Parent: no environment, no install, no crumb emission at all.
    unsetenv("GFXSTREAM_CRASH_TRACE");
    unsetenv("GFXSTREAM_CRASH_TRACE_FILE");
    expect(!trace::Enabled(), "default-off: Enabled() is false without the environment");
    expect(!trace::InstallIfEnabled(), "default-off: no handler is installed");

    const char* defaultPath = "/tmp/ahb-crash-trace-default-off-test.txt";
    std::remove(defaultPath);
    setenv("GFXSTREAM_CRASH_TRACE_FILE", defaultPath, 1);
    const uint32_t countBefore = trace::gBreadcrumbCount.load();
    trace::Breadcrumb("AHB-LIFE alloc enter", nullptr, 4096);
    trace::Breadcrumb("AHB-LIFE release", reinterpret_cast<const void*>(0x1234), 0);
    expect(trace::gBreadcrumbCount.load() == countBefore,
           "default-off: Breadcrumb() does not increment the ring counter");
    expect(readFile(defaultPath).empty(),
           "default-off: Breadcrumb() creates no trace file and writes nothing");
    unsetenv("GFXSTREAM_CRASH_TRACE_FILE");

    // Child A: install-once contract.
    pid_t pid = fork();
    if (pid == 0) {
        setenv("GFXSTREAM_CRASH_TRACE", "1", 1);
        trace::ResetEnabledCacheForTest();
        bool first = trace::InstallIfEnabled();
        bool second = trace::InstallIfEnabled();
        _exit((first && !second) ? 0 : 1);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    expect(WIFEXITED(status) && WEXITSTATUS(status) == 0,
           "install-once: first InstallIfEnabled() true, second false");

    // Child B: a real SIGSEGV must produce a stack, per-crumb tids, and a ring
    // that still holds the oldest of 70 crumbs (capacity >= 64).
    const char* tracePath = "/tmp/ahb-crash-trace-unit-test.txt";
    std::remove(tracePath);
    constexpr int kCrumbs = 70;
    pid = fork();
    if (pid == 0) {
        setenv("GFXSTREAM_CRASH_TRACE", "1", 1);
        setenv("GFXSTREAM_CRASH_TRACE_FILE", tracePath, 1);
        trace::ResetEnabledCacheForTest();
        trace::InstallIfEnabled();
        for (int i = 0; i < kCrumbs; ++i) {
            char site[64];
            std::snprintf(site, sizeof(site), "unit-test.marker-%d", i);
            trace::Breadcrumb(site, reinterpret_cast<const void*>(0x1000 + i), i);
        }
        raise(SIGSEGV);
        _exit(99);
    }
    waitpid(pid, &status, 0);
    expect(WIFSIGNALED(status) && WTERMSIG(status) == SIGSEGV,
           "handler re-raises SIGSEGV after printing");

    std::string text = readFile(tracePath);
    expect(text.find("[AHB-CRASH-TRACE] signal=11") != std::string::npos,
           "trace file contains the signal line");
    expect(text.find(" tid=") != std::string::npos,
           "breadcrumbs and the signal line carry tid=");
    expect(text.find("fault-pc") != std::string::npos &&
               text.find("unwind #00") != std::string::npos,
           "trace file contains fault PC and unwound frames");

    // The ring dump must contain exactly kRingSlots entries, newest 64, and
    // none of the 6 oldest crumbs. Only the ring section is inspected: the
    // ordinary breadcrumb log above it also contains every marker.
    size_t ringHeader = text.find("breadcrumbs (oldest first");
    std::string ring = ringHeader == std::string::npos ? std::string() : text.substr(ringHeader);
    expect(countOf(ring, "] ring #") == trace::kRingSlots,
           "trace file contains a full 64-entry ring");
    expect(ring.find("unit-test.marker-6 ") != std::string::npos &&
               ring.find("unit-test.marker-69 ") != std::string::npos,
           "ring keeps the whole 64-crumb window (marker-6 .. marker-69)");
    expect(ring.find("unit-test.marker-5 ") == std::string::npos,
           "ring drops crumbs older than the 64-entry window");

    // tid must be per-crumb and equal to the faulting thread's tid here.
    long long signalTid = -1;
    long long ringTid = -1;
    size_t signalPos = text.find("[AHB-CRASH-TRACE] signal=");
    if (signalPos != std::string::npos) {
        size_t signalEnd = text.find('\n', signalPos);
        signalTid = numberAfter(text.substr(signalPos, signalEnd - signalPos), "tid=");
    }
    size_t ringPos = ring.find("] ring #0 ");
    if (ringPos != std::string::npos) {
        size_t ringEnd = ring.find('\n', ringPos);
        ringTid = numberAfter(ring.substr(ringPos, ringEnd - ringPos), "tid=");
    }
    expect(signalTid > 0 && ringTid == signalTid,
           "the last crumb and the fault were written by the same tid");

    if (gFailures != 0) {
        std::printf("FAILED: %d check(s)\n", gFailures);
        return 1;
    }
    std::printf("ALL AHB-CRASH-TRACE UNIT TESTS PASSED\n");
    return 0;
}
