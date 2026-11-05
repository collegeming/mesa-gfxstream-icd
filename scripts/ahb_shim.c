/*
 * ahb_shim.c - LD_PRELOAD diagnostic shim for AHardwareBuffer_acquire/release.
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
 * Build (NDK r27c, same as build-host.yml):
 *   aarch64-linux-android33-clang -shared -fPIC -O2 -Wall -Wextra -Werror \
 *       -ldl scripts/ahb_shim.c -o libahb_shim.so
 *
 * Env:
 *   AHB_SHIM_LOG=/path             append log to /path (default: stderr)
 *   AHB_SHIM_TRACE_RELEASE=1       also log every non-NULL release (default: off)
 *
 * Rules honoured on both NULL paths: no malloc, no stdio stream, no lock of our
 * own.  They use __builtin_return_address(0), dladdr(), snprintf() into a stack
 * buffer and write(2).  The real symbol is resolved with dlsym(RTLD_NEXT) only
 * when it is about to be called, i.e. never on a NULL path.
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

typedef void AHardwareBuffer;

typedef void (*AhbFn)(AHardwareBuffer *);

#define AHB_SHIM_LINE_MAX 512U
/* A NULL release can repeat every frame; keep the first few lines only. */
#define AHB_SHIM_RELEASE_NULL_MAX 8U

static int g_log_fd = STDERR_FILENO;
static int g_trace_release = 0;
static int g_resolve_warned = 0;
static unsigned g_release_null_seen = 0;
static AhbFn g_real_acquire = NULL;
static AhbFn g_real_release = NULL;

static void shim_emit(const char *buf, int n)
{
    size_t len;
    size_t off = 0;

    if (n <= 0) {
        return;
    }
    len = (size_t)n;
    if (len >= AHB_SHIM_LINE_MAX) {
        len = AHB_SHIM_LINE_MAX - 1;
    }
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

static int shim_tid(void)
{
    return (int)syscall(SYS_gettid);
}

/* caller is passed in so that -O2 inlining can never skew
 * __builtin_return_address(0).  kind is "acquire-null" or "release-null". */
static void __attribute__((noinline))
shim_log_null(void *caller, const char *kind)
{
    Dl_info info;
    const char *module = "?";
    unsigned long offset = 0;
    char line[AHB_SHIM_LINE_MAX];
    int n;

    if (dladdr(caller, &info) != 0) {
        if (info.dli_fname != NULL && info.dli_fname[0] != '\0') {
            module = info.dli_fname;
        }
        if (info.dli_fbase != NULL) {
            offset = (unsigned long)((uintptr_t)caller -
                                     (uintptr_t)info.dli_fbase);
        }
    }

    n = snprintf(line, sizeof(line),
                 "AHB-SHIM %s caller=%p module=%s+0x%lx tid=%d\n",
                 kind, caller, module, offset, shim_tid());
    shim_emit(line, n);
}

static void __attribute__((noinline)) shim_log_acquire_null(void *caller)
{
    shim_log_null(caller, "acquire-null");
}

static void __attribute__((noinline)) shim_log_release_null(void *caller)
{
    unsigned seen = __atomic_fetch_add(&g_release_null_seen, 1,
                                       __ATOMIC_RELAXED);

    if (seen >= AHB_SHIM_RELEASE_NULL_MAX) {
        return;
    }
    shim_log_null(caller, "release-null");
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

__attribute__((constructor)) static void shim_ctor(void)
{
    const char *path = getenv("AHB_SHIM_LOG");
    const char *trace = getenv("AHB_SHIM_TRACE_RELEASE");
    char line[AHB_SHIM_LINE_MAX];
    int n;

    if (trace != NULL && trace[0] == '1' && trace[1] == '\0') {
        g_trace_release = 1;
    }
    if (path != NULL && path[0] != '\0') {
        int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
        if (fd >= 0) {
            g_log_fd = fd;
        }
    }

    /* One banner line so a run without any NULL event can still be told apart
     * from "LD_PRELOAD never reached this process"; shim=v2 identifies the
     * build when the same path is overwritten on device. */
    n = snprintf(line, sizeof(line),
                 "AHB-SHIM loaded pid=%d trace_release=%d log_fd=%d shim=v2\n",
                 (int)getpid(), g_trace_release, g_log_fd);
    shim_emit(line, n);
}
