// Diagnostic-only crash forensics for the gfxstream host backend.
//
// This header is copied into host/vulkan/ahb_crash_trace.h by
// scripts/patch_ahb_crash_stack.py on the diagnostic/ahb-crash-stack branch.
// It is not part of the shipping gfxstream source.
//
// Default state: OFF. Nothing is installed and no breadcrumb is written unless
// the process environment contains exactly GFXSTREAM_CRASH_TRACE=1. The
// compiled-in code costs one getenv() at library load and one branch per
// breadcrumb call; the handler itself does not allocate, format with stdio, or
// call dladdr. It prints raw PCs plus the module load offset computed from a
// /proc/self/maps snapshot taken at install time, so llvm-symbolizer can map
// every frame offline:
//
//   llvm-symbolizer -f -C -e <module> 0x<offset>
//
// Optional environment:
//   GFXSTREAM_CRASH_TRACE=1             enable (anything else keeps it off)
//   GFXSTREAM_CRASH_TRACE_FILE=<path>   also append the trace to <path>
//   GFXSTREAM_CRASH_MAPS_PATH=<path>    write the install-time maps snapshot
//
// The handler chains to the previously installed handler after printing, so
// crosvm's own crash handler still runs (it prints its register dump and calls
// _exit(128+sig), deliberately bypassing debuggerd).
#ifndef GFXSTREAM_HOST_VULKAN_AHB_CRASH_TRACE_H_
#define GFXSTREAM_HOST_VULKAN_AHB_CRASH_TRACE_H_

// Marker: this header enables the diagnostic implementation in every build that
// includes it. The branch that contains it is the diagnostic build.
#ifndef GFXSTREAM_DIAGNOSTIC_CRASH_TRACE
#define GFXSTREAM_DIAGNOSTIC_CRASH_TRACE 1
#endif

#if defined(GFXSTREAM_DIAGNOSTIC_CRASH_TRACE) && \
    (defined(__ANDROID__) || defined(GFXSTREAM_AHB_CRASH_TRACE_HOST_TEST))
#define GFXSTREAM_AHB_CRASH_TRACE_IMPL 1
#else
#define GFXSTREAM_AHB_CRASH_TRACE_IMPL 0
#endif

#if GFXSTREAM_AHB_CRASH_TRACE_IMPL

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <ucontext.h>
#include <unistd.h>
#include <unwind.h>

#include <atomic>

#include <sys/syscall.h>

namespace gfxstream {
namespace ahb_crash_trace {

// ---------------------------------------------------------------------------
// Tiny async-signal-safe formatter. No malloc, no stdio, no locale.
// ---------------------------------------------------------------------------
struct Writer {
    char* buf;
    size_t cap;
    size_t len;

    Writer(char* b, size_t c) : buf(b), cap(c), len(0) {}
    void ch(char c) {
        if (len + 1 < cap) buf[len++] = c;
    }
    void str(const char* s) {
        while (*s) ch(*s++);
    }
    void hex(uint64_t v) {
        char tmp[16];
        int n = 0;
        if (v == 0) {
            ch('0');
            return;
        }
        while (v != 0) {
            unsigned d = static_cast<unsigned>(v & 0xf);
            tmp[n++] = static_cast<char>(d < 10 ? '0' + d : 'a' + d - 10);
            v >>= 4;
        }
        while (n > 0) ch(tmp[--n]);
    }
    void dec(uint64_t v) {
        char tmp[24];
        int n = 0;
        if (v == 0) {
            ch('0');
            return;
        }
        while (v != 0) {
            tmp[n++] = static_cast<char>('0' + (v % 10));
            v /= 10;
        }
        while (n > 0) ch(tmp[--n]);
    }
    void finish() {
        if (len < cap) buf[len] = '\0';
        else if (cap > 0) buf[cap - 1] = '\0';
    }
};

inline long TraceGettid() {
#if defined(__ANDROID__)
    return static_cast<long>(gettid());
#else
    return static_cast<long>(syscall(SYS_gettid));
#endif
}

// ---------------------------------------------------------------------------
// Enable state and output fd. Resolved once per process.
// ---------------------------------------------------------------------------
inline std::atomic<int> gEnabledState{-1};
inline int gOutFd = STDERR_FILENO;

inline bool Enabled() {
    int state = gEnabledState.load(std::memory_order_acquire);
    if (state < 0) {
        const char* v = getenv("GFXSTREAM_CRASH_TRACE");
        state = (v != nullptr && v[0] == '1' && v[1] == '\0') ? 1 : 0;
        gEnabledState.store(state, std::memory_order_release);
    }
    return state == 1;
}

#if defined(GFXSTREAM_AHB_CRASH_TRACE_HOST_TEST)
// The enable decision is cached per process. A forked test child inherits the
// parent's cache, so tests reset it after changing the environment.
inline void ResetEnabledCacheForTest() {
    gEnabledState.store(-1, std::memory_order_release);
}
#endif

// ---------------------------------------------------------------------------
// Module table captured from /proc/self/maps at install time. Maps are merged
// per file: min start is the load bias, max end bounds the address range.
// ---------------------------------------------------------------------------
struct Module {
    uintptr_t start;
    uintptr_t end;
    uint32_t pathIndex;
};

constexpr uint32_t kMaxModules = 192;
constexpr size_t kMaxPath = 128;
constexpr size_t kMapsBufSize = 256 * 1024;

inline char gMapsBuf[kMapsBufSize];
inline Module gModules[kMaxModules];
inline char gModulePaths[kMaxModules][kMaxPath];
inline std::atomic<uint32_t> gModuleCount{0};
inline std::atomic<bool> gMapsReady{false};
inline std::atomic<bool> gRefreshedAtFirstBreadcrumb{false};

inline void WriteAll(int fd, const char* data, size_t len) {
    while (len > 0) {
        ssize_t n = write(fd, data, len);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return;
        data += n;
        len -= static_cast<size_t>(n);
    }
}

inline void CaptureMaps() {
    int fd = open("/proc/self/maps", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    size_t used = 0;
    while (used + 1 < sizeof(gMapsBuf)) {
        ssize_t n = read(fd, gMapsBuf + used, sizeof(gMapsBuf) - 1 - used);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        used += static_cast<size_t>(n);
    }
    close(fd);
    if (used == 0) return;
    gMapsBuf[used] = '\0';

    const char* mapsPath = getenv("GFXSTREAM_CRASH_MAPS_PATH");
    if (mapsPath != nullptr && mapsPath[0] != '\0') {
        int out = open(mapsPath, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (out >= 0) {
            WriteAll(out, gMapsBuf, used);
            close(out);
        }
    }

    uint32_t count = 0;
    char* line = gMapsBuf;
    while (line != nullptr && *line != '\0') {
        char* eol = strchr(line, '\n');
        if (eol != nullptr) *eol = '\0';

        unsigned long long start = 0;
        unsigned long long end = 0;
        char perms[8] = {};
        char path[kMaxPath] = {};
        int fieldCount = sscanf(line, "%llx-%llx %7s %*s %*s %*s %127s", &start, &end,
                                perms, path);
        if (fieldCount >= 4 && path[0] == '/' &&
            (strchr(perms, 'r') != nullptr || strchr(perms, 'x') != nullptr)) {
            bool found = false;
            for (uint32_t i = 0; i < count; ++i) {
                if (strcmp(gModulePaths[i], path) == 0) {
                    if (static_cast<uintptr_t>(start) < gModules[i].start) {
                        gModules[i].start = static_cast<uintptr_t>(start);
                    }
                    if (static_cast<uintptr_t>(end) > gModules[i].end) {
                        gModules[i].end = static_cast<uintptr_t>(end);
                    }
                    found = true;
                    break;
                }
            }
            if (!found && count < kMaxModules) {
                gModules[count].start = static_cast<uintptr_t>(start);
                gModules[count].end = static_cast<uintptr_t>(end);
                gModules[count].pathIndex = count;
                strncpy(gModulePaths[count], path, kMaxPath - 1);
                gModulePaths[count][kMaxPath - 1] = '\0';
                ++count;
            }
        }
        line = eol != nullptr ? eol + 1 : nullptr;
    }
    gModuleCount.store(count, std::memory_order_release);
    gMapsReady.store(true, std::memory_order_release);
}

inline const char* ModuleFor(uintptr_t pc, uintptr_t* offset) {
    if (!gMapsReady.load(std::memory_order_acquire)) {
        return nullptr;
    }
    uint32_t count = gModuleCount.load(std::memory_order_acquire);
    for (uint32_t i = 0; i < count; ++i) {
        if (pc >= gModules[i].start && pc < gModules[i].end) {
            *offset = pc - gModules[i].start;
            return gModulePaths[i];
        }
    }
    return nullptr;
}

// ---------------------------------------------------------------------------
// Last-N breadcrumb ring. Writers are ordinary threads; the handler is the
// only reader. A release/acquire length keeps a slot from being read half
// written; a concurrent overwrite can still mix fields, which is acceptable
// for a diagnostic.
// ---------------------------------------------------------------------------
constexpr uint32_t kRingSlots = 16;
constexpr size_t kRingLine = 128;
inline char gRing[kRingSlots][kRingLine];
inline std::atomic<uint8_t> gRingLen[kRingSlots];
inline std::atomic<uint32_t> gRingSeq{0};
inline std::atomic<uint32_t> gBreadcrumbCount{0};

// ---------------------------------------------------------------------------
// Signal handler. Only async-signal-safe calls: write(2), gettid(), memcpy,
// strlen, _Unwind_Backtrace (bounded), sigaction, raise, _exit.
// ---------------------------------------------------------------------------
struct UnwindState {
    uintptr_t pcs[48];
    int count;
};

inline _Unwind_Reason_Code UnwindCallback(struct _Unwind_Context* context, void* arg) {
    UnwindState* state = static_cast<UnwindState*>(arg);
    uintptr_t ip = static_cast<uintptr_t>(_Unwind_GetIP(context));
    if (ip != 0 && state->count < static_cast<int>(sizeof(state->pcs) / sizeof(state->pcs[0]))) {
        state->pcs[state->count++] = ip;
    }
    return state->count >= static_cast<int>(sizeof(state->pcs) / sizeof(state->pcs[0]))
               ? _URC_END_OF_STACK
               : _URC_NO_REASON;
}

struct SignalSlot {
    int signal;
    struct sigaction previous;
    bool valid;
};

inline SignalSlot gSlots[4] = {{SIGSEGV, {}, false}, {SIGBUS, {}, false}, {SIGILL, {}, false},
                               {SIGFPE, {}, false}};
inline std::atomic<bool> gInstalled{false};

inline SignalSlot* SlotFor(int sig) {
    for (SignalSlot& slot : gSlots) {
        if (slot.signal == sig) return &slot;
    }
    return nullptr;
}

inline void WriteFrame(const char* kind, int index, uintptr_t pc) {
    char line[224];
    Writer w(line, sizeof(line));
    w.str("[AHB-CRASH-TRACE] ");
    w.str(kind);
    w.str(" #");
    if (index < 10) w.ch('0');
    w.dec(static_cast<uint64_t>(index));
    w.str(" pc=0x");
    w.hex(pc);
    uintptr_t offset = 0;
    const char* module = ModuleFor(pc, &offset);
    if (module != nullptr) {
        w.str(" ");
        w.str(module);
        w.str("+0x");
        w.hex(offset);
    }
    w.str("\n");
    w.finish();
    WriteAll(gOutFd, line, w.len);
}

inline void DumpRing() {
    char line[192];
    Writer header(line, sizeof(line));
    header.str("[AHB-CRASH-TRACE] breadcrumbs (oldest first, last 16):\n");
    header.finish();
    WriteAll(gOutFd, line, header.len);

    uint32_t seq = gRingSeq.load(std::memory_order_acquire);
    uint32_t available = seq < kRingSlots ? seq : kRingSlots;
    for (uint32_t i = 0; i < available; ++i) {
        uint32_t slot = (seq - available + i) & (kRingSlots - 1);
        uint8_t len = gRingLen[slot].load(std::memory_order_acquire);
        if (len == 0) continue;
        Writer w(line, sizeof(line));
        w.str("[AHB-CRASH-TRACE] ring #");
        w.dec(i);
        w.str(" ");
        size_t n = len < kRingLine - 1 ? len : kRingLine - 1;
        for (size_t j = 0; j < n; ++j) w.ch(gRing[slot][j]);
        w.str("\n");
        w.finish();
        WriteAll(gOutFd, line, w.len);
    }
}

extern "C" void GfxAhbCrashTraceHandler(int sig, siginfo_t* info, void* ucontextRaw) {
    int savedErrno = errno;
    uintptr_t fault = info != nullptr ? reinterpret_cast<uintptr_t>(info->si_addr) : 0;
    uintptr_t pc = 0;
    uintptr_t lr = 0;
    uintptr_t sp = 0;
    uintptr_t x0 = 0;
    uintptr_t x1 = 0;
    uintptr_t x2 = 0;
    uintptr_t x3 = 0;

#if defined(__aarch64__)
    if (ucontextRaw != nullptr) {
        ucontext_t* uc = static_cast<ucontext_t*>(ucontextRaw);
        pc = static_cast<uintptr_t>(uc->uc_mcontext.pc);
        lr = static_cast<uintptr_t>(uc->uc_mcontext.regs[30]);
        sp = static_cast<uintptr_t>(uc->uc_mcontext.sp);
        x0 = static_cast<uintptr_t>(uc->uc_mcontext.regs[0]);
        x1 = static_cast<uintptr_t>(uc->uc_mcontext.regs[1]);
        x2 = static_cast<uintptr_t>(uc->uc_mcontext.regs[2]);
        x3 = static_cast<uintptr_t>(uc->uc_mcontext.regs[3]);
    }
#elif defined(__x86_64__)
    if (ucontextRaw != nullptr) {
        ucontext_t* uc = static_cast<ucontext_t*>(ucontextRaw);
        pc = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RIP]);
        lr = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RBP]);
        sp = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RSP]);
        x0 = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RDI]);
        x1 = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RSI]);
        x2 = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RDX]);
        x3 = static_cast<uintptr_t>(uc->uc_mcontext.gregs[REG_RCX]);
    }
#endif

    {
        char line[320];
        Writer w(line, sizeof(line));
        w.str("[AHB-CRASH-TRACE] signal=");
        w.dec(static_cast<uint64_t>(sig));
        w.str(" tid=");
        w.dec(static_cast<uint64_t>(TraceGettid()));
        w.str(" fault=0x");
        w.hex(fault);
        w.str(" pc=0x");
        w.hex(pc);
        w.str(" lr=0x");
        w.hex(lr);
        w.str(" sp=0x");
        w.hex(sp);
        w.str("\n");
        w.finish();
        WriteAll(gOutFd, line, w.len);
    }
    {
        char line[320];
        Writer w(line, sizeof(line));
        w.str("[AHB-CRASH-TRACE] x0=0x");
        w.hex(x0);
        w.str(" x1=0x");
        w.hex(x1);
        w.str(" x2=0x");
        w.hex(x2);
        w.str(" x3=0x");
        w.hex(x3);
        w.str("\n");
        w.finish();
        WriteAll(gOutFd, line, w.len);
    }

    WriteFrame("fault-pc", 0, pc);
    if (lr != 0 && lr != pc) WriteFrame("fault-lr", 0, lr);

    UnwindState state = {};
    _Unwind_Backtrace(&UnwindCallback, &state);
    for (int i = 0; i < state.count; ++i) {
        WriteFrame("unwind", i, state.pcs[i]);
    }

#if defined(__aarch64__)
    // _Unwind_Backtrace starts in this handler and may stop at the signal
    // trampoline. The interrupted frame's return addresses are also present on
    // the stack below the signal frame. Scan a bounded window and print only
    // words that resolve into a mapped module, so heap values do not drown the
    // useful candidates. This is what recovers the caller of
    // AHardwareBuffer_acquire when the unwinder cannot cross the trampoline.
    if (sp != 0) {
        const uintptr_t* stack = reinterpret_cast<const uintptr_t*>(sp);
        for (int i = 0; i < 128; ++i) {
            uintptr_t candidate = stack[i];
            uintptr_t candidateOffset = 0;
            if (candidate != 0 && ModuleFor(candidate, &candidateOffset) != nullptr) {
                WriteFrame("stack", i, candidate);
            }
        }
    }
#endif

#if defined(__aarch64__)
    if (ucontextRaw != nullptr) {
        ucontext_t* uc = static_cast<ucontext_t*>(ucontextRaw);
        uintptr_t fp = static_cast<uintptr_t>(uc->uc_mcontext.regs[29]);
        for (int i = 0; i < 32 && fp != 0; ++i) {
            if ((fp & 0x7) != 0 || sp == 0 || fp < sp || fp - sp > (1u << 20)) break;
            uintptr_t next = *reinterpret_cast<uintptr_t*>(fp);
            uintptr_t ret = *reinterpret_cast<uintptr_t*>(fp + 8);
            if (ret == 0) break;
            WriteFrame("fp", i, ret);
            if (next <= fp) break;
            fp = next;
        }
    }
#endif

    DumpRing();

    SignalSlot* slot = SlotFor(sig);
    if (slot != nullptr && slot->valid) {
        const struct sigaction& previous = slot->previous;
        if ((previous.sa_flags & SA_SIGINFO) != 0 && previous.sa_sigaction != nullptr &&
            previous.sa_sigaction != GfxAhbCrashTraceHandler) {
            previous.sa_sigaction(sig, info, ucontextRaw);
            errno = savedErrno;
            return;
        }
        if ((previous.sa_flags & SA_SIGINFO) == 0 && previous.sa_handler != SIG_DFL &&
            previous.sa_handler != SIG_IGN) {
            previous.sa_handler(sig);
            errno = savedErrno;
            return;
        }
    }

    // No usable predecessor: restore the default action and re-raise so the
    // process dies with the same signal it would have without this handler.
    struct sigaction dfl = {};
    dfl.sa_handler = SIG_DFL;
    sigemptyset(&dfl.sa_mask);
    sigaction(sig, &dfl, nullptr);
    raise(sig);
    errno = savedErrno;
}

inline void InstallOne(int sig) {
    SignalSlot* slot = SlotFor(sig);
    if (slot == nullptr) return;
    struct sigaction action = {};
    action.sa_sigaction = GfxAhbCrashTraceHandler;
    action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    struct sigaction previous = {};
    if (sigaction(sig, &action, &previous) == 0) {
        slot->previous = previous;
        slot->valid = true;
    }
}

inline bool InstallIfEnabled() {
    if (!Enabled()) return false;
    bool expected = false;
    if (!gInstalled.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
        return false;
    }
    const char* outPath = getenv("GFXSTREAM_CRASH_TRACE_FILE");
    if (outPath != nullptr && outPath[0] != '\0') {
        int fd = open(outPath, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
        if (fd >= 0) gOutFd = fd;
    }
    CaptureMaps();
    InstallOne(SIGSEGV);
    InstallOne(SIGBUS);
    InstallOne(SIGILL);
    InstallOne(SIGFPE);
    return true;
}

// Re-assert the handler if another component replaced it after our install
// (crosvm installs its own handler in main(), normally before the backend is
// loaded, but do not depend on that order).
inline void EnsureInstalled() {
    if (!Enabled()) return;
    if (!gInstalled.load(std::memory_order_acquire)) {
        InstallIfEnabled();
        return;
    }
    // The constructor runs when libgfxstream_backend.so is loaded. The Mali
    // driver and the decoder's threads may appear after that. Refresh the maps
    // snapshot once, at the first AHB breadcrumb, when the processes' modules
    // are all present.
    if (!gRefreshedAtFirstBreadcrumb.exchange(true, std::memory_order_acq_rel)) {
        CaptureMaps();
    }
    struct sigaction current = {};
    if (sigaction(SIGSEGV, nullptr, &current) == 0) {
        bool ours = (current.sa_flags & SA_SIGINFO) != 0 &&
                    current.sa_sigaction == GfxAhbCrashTraceHandler;
        if (!ours) {
            gInstalled.store(false, std::memory_order_release);
            InstallIfEnabled();
        }
    }
}

inline void Breadcrumb(const char* site, const void* pointer, unsigned long long value) {
    if (!Enabled()) return;
    EnsureInstalled();
    // Bound the ordinary-context log so a hot path cannot flood it.
    if (gBreadcrumbCount.fetch_add(1, std::memory_order_acq_rel) >= 512) return;

    char payload[112];
    Writer w(payload, sizeof(payload));
    w.str("site=");
    w.str(site != nullptr ? site : "?");
    w.str(" ptr=0x");
    w.hex(reinterpret_cast<uintptr_t>(pointer));
    w.str(" v=0x");
    w.hex(static_cast<uint64_t>(value));
    w.finish();

    uint32_t seq = gRingSeq.fetch_add(1, std::memory_order_acq_rel);
    uint32_t slot = seq & (kRingSlots - 1);
    size_t n = w.len < kRingLine - 1 ? w.len : kRingLine - 1;
    memcpy(gRing[slot], payload, n);
    gRing[slot][n] = '\0';
    gRingLen[slot].store(static_cast<uint8_t>(n), std::memory_order_release);

    char line[192];
    Writer out(line, sizeof(line));
    out.str("[AHB-CRASH-TRACE] ");
    out.str(payload);
    out.str("\n");
    out.finish();
    WriteAll(gOutFd, line, out.len);
}

// Install at library load when the environment asks for it, so even a fault
// before the first AHB call is captured.
__attribute__((constructor)) static void GfxAhbCrashTraceConstructor() {
    if (Enabled()) {
        InstallIfEnabled();
    }
}

}  // namespace ahb_crash_trace
}  // namespace gfxstream

#define GFXSTREAM_AHB_TRACE(site, pointer, value)                 \
    ::gfxstream::ahb_crash_trace::Breadcrumb((site), (pointer), \
                                             static_cast<unsigned long long>(value))

#else  // !GFXSTREAM_AHB_CRASH_TRACE_IMPL

// Non-diagnostic builds: the breadcrumb macro must still compile.
#define GFXSTREAM_AHB_TRACE(site, pointer, value) \
    do {                                          \
    } while (0)

#endif  // GFXSTREAM_AHB_CRASH_TRACE_IMPL

#endif  // GFXSTREAM_HOST_VULKAN_AHB_CRASH_TRACE_H_
