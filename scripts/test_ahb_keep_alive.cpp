// Host-side unit test for the ColorBuffer AHB keep-alive switch.
//
// Build (CI does this):
//   c++ -std=c++17 -pthread -Wall -Wextra -Werror -fno-omit-frame-pointer
//       -I source/host/vulkan scripts/test_ahb_keep_alive.cpp -o test-ahb-keep-alive
//
// Proves the switch is opt-in and per-call, and that the AHB-KEEP-ALIVE line is
// rate-limited. The skip/release branch itself is asserted statically in
// test_ahb_keep_alive.py because it needs a real AHardwareBuffer.

#define GFXSTREAM_AHB_KEEP_ALIVE_HOST_TEST 1
#include "ahb_keep_alive.h"

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace keepalive = gfxstream::ahb_keep_alive;

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

int main() {
    // Exact opt-in: only the value "1" enables the switch.
    unsetenv("GFXSTREAM_AHB_KEEP_ALIVE");
    expect(!keepalive::Enabled(), "unset: keep-alive is off");
    setenv("GFXSTREAM_AHB_KEEP_ALIVE", "0", 1);
    expect(!keepalive::Enabled(), "value 0: keep-alive is off");
    setenv("GFXSTREAM_AHB_KEEP_ALIVE", "11", 1);
    expect(!keepalive::Enabled(), "value 11: keep-alive is off (exact match)");
    setenv("GFXSTREAM_AHB_KEEP_ALIVE", "1 ", 1);
    expect(!keepalive::Enabled(), "trailing space: keep-alive is off");
    setenv("GFXSTREAM_AHB_KEEP_ALIVE", "1", 1);
    expect(keepalive::Enabled(), "value 1: keep-alive is on");

    // No cached static: flipping the environment between calls is visible.
    unsetenv("GFXSTREAM_AHB_KEEP_ALIVE");
    expect(!keepalive::Enabled(), "cache check: unset after 1 reads as off");
    setenv("GFXSTREAM_AHB_KEEP_ALIVE", "1", 1);
    expect(keepalive::Enabled(), "cache check: 1 after unset reads as on (per-call getenv)");

    // Rate limit: first 16 skips log, later skips are silent.
    const char* logPath = "/tmp/ahb-keep-alive-unit-test.txt";
    std::remove(logPath);
    pid_t pid = fork();
    if (pid == 0) {
        FILE* out = std::fopen(logPath, "w");
        if (out == nullptr) _exit(2);
        int saved = dup(2);
        dup2(fileno(out), 2);
        int logged = 0;
        for (int i = 0; i < 20; ++i) {
            if (keepalive::LogSkip(reinterpret_cast<const void*>(0x1000 + i))) ++logged;
        }
        std::fflush(stderr);
        dup2(saved, 2);
        std::fclose(out);
        _exit(logged == 16 ? 0 : 1);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    expect(WIFEXITED(status) && WEXITSTATUS(status) == 0,
           "rate limit: exactly 16 of 20 LogSkip calls return true");

    std::string text = readFile(logPath);
    expect(countOf(text, "AHB-KEEP-ALIVE ptr=0x") == 16,
           "log output: 16 AHB-KEEP-ALIVE lines");
    expect(text.find(" tid=") != std::string::npos,
           "log output: each line carries tid=");

    if (gFailures != 0) {
        std::printf("FAILED: %d check(s)\n", gFailures);
        return 1;
    }
    std::printf("ALL AHB-KEEP-ALIVE UNIT TESTS PASSED\n");
    return 0;
}
