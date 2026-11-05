// Host-side unit test for the diagnostic AHB crash stack tracer.
//
// Build (CI does this):
//   c++ -std=c++17 -pthread -Wall -Wextra -Werror -fno-omit-frame-pointer
//       -I source/host/vulkan scripts/test_ahb_crash_stack.cpp -o test-ahb-crash-stack
//
// The test proves the two properties that make the tracer safe to ship in a
// diagnostic build: it is fully off unless GFXSTREAM_CRASH_TRACE=1, and the
// handler installs only once. A forked child then faults with SIGSEGV and the
// parent checks that a stack and the breadcrumb ring reached the trace file.

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

int main() {
    // Parent: no environment, no install, no behaviour change.
    unsetenv("GFXSTREAM_CRASH_TRACE");
    unsetenv("GFXSTREAM_CRASH_TRACE_FILE");
    expect(!trace::Enabled(), "default-off: Enabled() is false without the environment");
    expect(!trace::InstallIfEnabled(), "default-off: no handler is installed");

    // Child A: install-once contract.
    pid_t pid = fork();
    if (pid == 0) {
        setenv("GFXSTREAM_CRASH_TRACE", "1", 1);
        bool first = trace::InstallIfEnabled();
        bool second = trace::InstallIfEnabled();
        _exit((first && !second) ? 0 : 1);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    expect(WIFEXITED(status) && WEXITSTATUS(status) == 0,
           "install-once: first InstallIfEnabled() true, second false");

    // Child B: a real SIGSEGV must produce a stack and the ring in one file.
    const char* tracePath = "/tmp/ahb-crash-trace-unit-test.txt";
    std::remove(tracePath);
    pid = fork();
    if (pid == 0) {
        setenv("GFXSTREAM_CRASH_TRACE", "1", 1);
        setenv("GFXSTREAM_CRASH_TRACE_FILE", tracePath, 1);
        trace::InstallIfEnabled();
        trace::Breadcrumb("unit-test.allocAhb.enter", nullptr, 65536);
        trace::Breadcrumb("unit-test.import", reinterpret_cast<const void*>(0x1234), 7);
        raise(SIGSEGV);
        _exit(99);
    }
    waitpid(pid, &status, 0);
    expect(WIFSIGNALED(status) && WTERMSIG(status) == SIGSEGV,
           "handler re-raises SIGSEGV after printing");

    std::string text = readFile(tracePath);
    expect(text.find("[AHB-CRASH-TRACE] signal=11") != std::string::npos,
           "trace file contains the signal line");
    expect(text.find("fault-pc") != std::string::npos &&
               text.find("unwind #00") != std::string::npos,
           "trace file contains fault PC and unwound frames");
    expect(text.find("unit-test.allocAhb.enter") != std::string::npos &&
               text.find("unit-test.import") != std::string::npos,
           "trace file contains the last breadcrumbs");
    expect(text.find("ring #0") != std::string::npos,
           "trace file contains the breadcrumb ring");

    if (gFailures != 0) {
        std::printf("FAILED: %d check(s)\n", gFailures);
        return 1;
    }
    std::printf("ALL AHB-CRASH-TRACE UNIT TESTS PASSED\n");
    return 0;
}
