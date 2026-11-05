/*
 * ahb_shim.c - LD_PRELOAD diagnostic shim for AHardwareBuffer_acquire/release.
 *
 * v3: a NULL argument is still dropped (libnativewindow dereferences NULL in
 *     incStrong/decStrong), but the first 8 NULL events per process now log the
 *     caller line *plus* up to AHB_SHIM_FRAMES backtrace frames, each resolved
 *     with dladdr() as "module=/path+0xoffset".  Later NULL events log a single
 *     "suppressed=N" line so a repeating driver bug cannot flood the log.
 *     Frames are collected with _Unwind_Backtrace (eh_frame based), which also
 *     walks vendor modules such as libGLES_mali.so when they carry unwind info.
 *     If no frame can be resolved, only the v2 caller line is printed.
 *
 * v2: a NULL release is logged (rate limited) and dropped.  libnativewindow's
 *     release tail-calls RefBase::decStrong() without a NULL check either, so
 *     forwarding NULL faults exactly like the v1 acquire case did.
 *
 * Why (v1): crosvm died in AHardwareBuffer_acquire(0) -> RefBase::incStrong
 * (fault 0x8).  AGENT-AHB-SYMBOLS.md shows acquire() has no NULL check, and
 * AGENT-AHB-CRASH-STACK.md 8/9 shows the crashing thread carries no gfxstream
 * AHB breadcrumb.  On device the v1 shim caught
 *   module=/vendor/lib64/egl/libGLES_mali.so+0x128b3cc
 * three times, so the caller is the Mali driver.
 *
 * Build (NDK r27c, as in .github/workflows/build-ahb-shim.yml): compile with
 * -fno-omit-frame-pointer -funwind-tables.  _Unwind_Backtrace is exported by
 * bionic libc itself (LIBC_Q, API 30), so no libunwind.a is linked in; that
 * archive would also drag malloc/stdio/pthread_rwlock imports into the shim.
 *
 * Env:
 *   AHB_SHIM_LOG=/path             append log to /path (default: stderr)
 *   AHB_SHIM_FRAMES=N              0..16 frames per NULL backtrace (default: 16)
 *   AHB_SHIM_TRACE_RELEASE=1       also log every non-NULL release (default: off)
 *
 * Rules honoured on both NULL paths: no malloc, no stdio stream, no lock of our
 * own.  They use __builtin_return_address(0), _Unwind_Backtrace(), dladdr(),
 * snprintf() into stack buffers and write(2).  The real symbol is resolved with
 * dlsym(RTLD_NEXT) only when it is about to be called, i.e. never on a NULL
 * path; the NULL path never reaches it.
 */
#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>
#include <unwind.h>

typedef void AHardwareBuffer;

typedef void (*AhbFn)(AHardwareBuffer *);

#define AHB_SHIM_LINE_MAX 512U
/* Whole multi-line block (caller + up to 16 frames) is built on the stack. */
#define AHB_SHIM_BLOCK_MAX 12288U
#define AHB_SHIM_FRAMES_DEFAULT 16U
#define AHB_SHIM_FRAMES_MAX 16U
/* Full backtraces per process; later NULL events keep one summary line. */
#define AHB_SHIM_TRACE_MAX 8U

static int g_log_fd = STDERR_FILENO;
static int g_trace_release = 0;
static int g_resolve_warned = 0;
static unsigned g_frames = AHB_SHIM_FRAMES_DEFAULT;
static uintptr_t g_self_base = 0;
static unsigned g_acquire_null_seen = 0;
static unsigned g_release_null_seen = 0;
static AhbFn g_real_acquire = NULL;
static AhbFn g_real_release = NULL;

struct shim_bt {
    uintptr_t pc[AHB_SHIM_FRAMES_MAX];
    unsigned count;
    unsigned max;
    uintptr_t self_base;
};

static void shim_emit(const char *buf, int n)
{
    size_t len;
    size_t off = 0;

    if (n <= 0) {
        return;
    }
    len = (size_t)n;
    while (off < len) {
        ssize_t w = write(g_log_fd, buf + off, len - off);
        if (w > 0) {
            off += (size_t)w;
        } else if (w < 0 && errno == EINTR) {
            continue;
        } else {
            break;
        }
    }
}

/* Track how much of a stack block snprintf() actually used.  A truncated
 * snprintf() still returns the would-be length, so clamp to the buffer end. */
static size_t shim_append(size_t cap, size_t off, int n)
{
    size_t add;

    if (n <= 0 || off >= cap) {
        return off;
    }
    add = (size_t)n;
    if (add >= cap - off) {
        return cap - 1;
    }
    return off + add;
}

static int shim_tid(void)
{
    return (int)syscall(SYS_gettid);
}

static _Unwind_Reason_Code shim_bt_cb(struct _Unwind_Context *ctx, void *arg)
{
    struct shim_bt *bt = (struct shim_bt *)arg;
    uintptr_t pc = (uintptr_t)_Unwind_GetIP(ctx);
    Dl_info info;

    if (pc == 0) {
        return _URC_NO_REASON;
    }

    /* Drop the shim's own frames (acquire -> logger -> _Unwind_Backtrace) so
     * frame #00 is the first real caller, i.e. the Mali call site. */
    if (bt->self_base != 0 && dladdr((void *)pc, &info) != 0 &&
        (uintptr_t)info.dli_fbase == bt->self_base) {
        return _URC_NO_REASON;
    }

    if (bt->count < bt->max) {
        bt->pc[bt->count++] = pc;
    }
    if (bt->count >= bt->max) {
        return _URC_END_OF_STACK;
    }
    return _URC_NO_REASON;
}

/* caller is passed in so that -O2 inlining can never skew
 * __builtin_return_address(0).  kind is "acquire-null" or "release-null". */
static void __attribute__((noinline))
shim_log_null(void *caller, const char *kind, unsigned *seen)
{
    Dl_info info;
    const char *module = "?";
    unsigned long offset = 0;
    struct shim_bt bt;
    unsigned index;
    unsigned printed = 0;
    unsigned i;
    char block[AHB_SHIM_BLOCK_MAX];
    size_t off = 0;
    int n;

    index = __atomic_fetch_add(seen, 1, __ATOMIC_RELAXED);
    if (index >= AHB_SHIM_TRACE_MAX) {
        n = snprintf(block, sizeof(block),
                     "AHB-SHIM %s suppressed=%u tid=%d\n",
                     kind, index + 1 - AHB_SHIM_TRACE_MAX, shim_tid());
        shim_emit(block, n);
        return;
    }

    if (dladdr(caller, &info) != 0) {
        if (info.dli_fname != NULL && info.dli_fname[0] != '\0') {
            module = info.dli_fname;
        }
        if (info.dli_fbase != NULL) {
            offset = (unsigned long)((uintptr_t)caller -
                                     (uintptr_t)info.dli_fbase);
        }
    }

    n = snprintf(block, sizeof(block),
                 "AHB-SHIM %s caller=%p module=%s+0x%lx tid=%d\n",
                 kind, caller, module, offset, shim_tid());
    off = shim_append(sizeof(block), 0, n);

    bt.count = 0;
    bt.max = g_frames;
    bt.self_base = g_self_base;
    if (bt.max > 0) {
        (void)_Unwind_Backtrace(shim_bt_cb, &bt);
    }

    for (i = 0; i < bt.count && off + 1 < sizeof(block); i++) {
        Dl_info frame_info;
        const char *frame_module;
        unsigned long frame_offset = 0;

        /* A frame that dladdr() cannot place is skipped; if that leaves no
         * frames at all the block keeps just the caller line above. */
        if (dladdr((void *)bt.pc[i], &frame_info) == 0) {
            continue;
        }
        if (frame_info.dli_fname == NULL || frame_info.dli_fname[0] == '\0') {
            continue;
        }
        frame_module = frame_info.dli_fname;
        if (frame_info.dli_fbase != NULL) {
            frame_offset = (unsigned long)(bt.pc[i] -
                                           (uintptr_t)frame_info.dli_fbase);
        }
        n = snprintf(block + off, sizeof(block) - off,
                     "#%02u pc=0x%lx module=%s+0x%lx\n",
                     printed, (unsigned long)bt.pc[i], frame_module,
                     frame_offset);
        if (n <= 0) {
            break;
        }
        off = shim_append(sizeof(block), off, n);
        printed++;
    }

    shim_emit(block, (int)off);
}

static void __attribute__((noinline)) shim_log_acquire_null(void *caller)
{
    shim_log_null(caller, "acquire-null", &g_acquire_null_seen);
}

static void __attribute__((noinline)) shim_log_release_null(void *caller)
{
    shim_log_null(caller, "release-null", &g_release_null_seen);
}

static void shim_log_release(AHardwareBuffer *buffer)
{
    char line[AHB_SHIM_LINE_MAX];
    int n;

    n = snprintf(line, sizeof(line), "AHB-SHIM release ptr=%p tid=%d\n",
                 (void *)buffer, shim_tid());
    shim_emit(line, n);
}

static void shim_log_resolve_failed(const char *name)
{
    char line[AHB_SHIM_LINE_MAX];
    int n;

    if (__atomic_exchange_n(&g_resolve_warned, 1, __ATOMIC_RELAXED) != 0) {
        return;
    }
    n = snprintf(line, sizeof(line),
                 "AHB-SHIM resolve-failed symbol=%s tid=%d (real call dropped)\n",
                 name, shim_tid());
    shim_emit(line, n);
}

void AHardwareBuffer_acquire(AHardwareBuffer *buffer)
{
    if (buffer == NULL) {
        /* Keep the address of the caller before returning.  The real acquire
         * has no NULL check: fromAHardwareBuffer(0) then incStrong(0) faults
         * at libutils+0x11a50 (fault 0x8).  Do not call it. */
        void *caller = __builtin_return_address(0);
        shim_log_acquire_null(caller);
        return;
    }

    if (g_real_acquire == NULL) {
        g_real_acquire = (AhbFn)dlsym(RTLD_NEXT, "AHardwareBuffer_acquire");
        if (g_real_acquire == NULL) {
            shim_log_resolve_failed("AHardwareBuffer_acquire");
            return;
        }
    }
    g_real_acquire(buffer);
}

void AHardwareBuffer_release(AHardwareBuffer *buffer)
{
    if (buffer == NULL) {
        /* Same hazard as acquire: libnativewindow's release has no NULL check
         * and tail-calls decStrong(0), which faults on [x0 + 8]. */
        void *caller = __builtin_return_address(0);
        shim_log_release_null(caller);
        return;
    }

    if (g_trace_release != 0) {
        shim_log_release(buffer);
    }

    if (g_real_release == NULL) {
        g_real_release = (AhbFn)dlsym(RTLD_NEXT, "AHardwareBuffer_release");
        if (g_real_release == NULL) {
            shim_log_resolve_failed("AHardwareBuffer_release");
            return;
        }
    }
    g_real_release(buffer);
}

static unsigned shim_parse_frames(const char *value)
{
    unsigned frames = 0;
    const char *p = value;

    if (p == NULL || *p < '0' || *p > '9') {
        return AHB_SHIM_FRAMES_DEFAULT;
    }
    while (*p >= '0' && *p <= '9') {
        frames = frames * 10 + (unsigned)(*p - '0');
        if (frames > AHB_SHIM_FRAMES_MAX) {
            return AHB_SHIM_FRAMES_MAX;
        }
        p++;
    }
    return frames;
}

__attribute__((constructor)) static void shim_ctor(void)
{
    const char *path = getenv("AHB_SHIM_LOG");
    const char *trace = getenv("AHB_SHIM_TRACE_RELEASE");
    const char *frames = getenv("AHB_SHIM_FRAMES");
    Dl_info self;
    char line[AHB_SHIM_LINE_MAX];
    int n;

    if (trace != NULL && trace[0] == '1' && trace[1] == '\0') {
        g_trace_release = 1;
    }
    g_frames = shim_parse_frames(frames);
    if (dladdr((void *)&shim_emit, &self) != 0 && self.dli_fbase != NULL) {
        g_self_base = (uintptr_t)self.dli_fbase;
    }
    if (path != NULL && path[0] != '\0') {
        int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
        if (fd >= 0) {
            g_log_fd = fd;
        }
    }

    /* One banner line so a run without any NULL event can still be told apart
     * from "LD_PRELOAD never reached this process"; shim=v3 identifies the
     * build when the same path is overwritten on device. */
    n = snprintf(line, sizeof(line),
                 "AHB-SHIM loaded pid=%d trace_release=%d log_fd=%d "
                 "frames=%u shim=v3\n",
                 (int)getpid(), g_trace_release, g_log_fd, g_frames);
    shim_emit(line, n);
}
