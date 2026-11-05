/*
 * Host-only unit test driver for scripts/ahb_shim.c.  Never shipped to the
 * device; it is compiled twice in CI:
 *
 *   cc -shared -fPIC -O2 -Wall -Wextra -Werror -DAHB_SHIM_STUB \
 *      -o libahb_stub.so scripts/ahb_shim_test.c
 *   cc -O2 -Wall -Wextra -Werror -o ahb_shim_test scripts/ahb_shim_test.c \
 *      -L. -lahb_stub -Wl,-rpath,'$ORIGIN'
 *   LD_PRELOAD=./libahb_shim_host.so ./ahb_shim_test <case>
 *
 * cases: null-release, null-release-many, null-acquire, non-null
 *
 * The fake real implementation lives in a separate DSO on purpose:
 * dlsym(RTLD_NEXT) from the preloaded shim only searches objects after the
 * shim in the global scope, so a definition in the main executable would not
 * be found.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef void AHardwareBuffer;

#ifdef AHB_SHIM_STUB

static int g_acquire_calls;
static int g_release_calls;

void AHardwareBuffer_acquire(AHardwareBuffer *buffer)
{
    g_acquire_calls++;
    printf("REAL acquire %p\n", (void *)buffer);
}

void AHardwareBuffer_release(AHardwareBuffer *buffer)
{
    g_release_calls++;
    printf("REAL release %p\n", (void *)buffer);
}

void ahb_stub_counts(int *acquire_calls, int *release_calls)
{
    *acquire_calls = g_acquire_calls;
    *release_calls = g_release_calls;
}

#else /* driver */

void ahb_stub_counts(int *acquire_calls, int *release_calls);
void AHardwareBuffer_acquire(AHardwareBuffer *buffer);
void AHardwareBuffer_release(AHardwareBuffer *buffer);

int main(int argc, char **argv)
{
    const char *name = (argc > 1) ? argv[1] : "";
    void *fake_ahb = (void *)(uintptr_t)0x1234;
    int acquire_calls = -1;
    int release_calls = -1;
    int i;

    if (strcmp(name, "null-release") == 0) {
        AHardwareBuffer_release(NULL);
    } else if (strcmp(name, "null-release-many") == 0) {
        for (i = 0; i < 30; i++) {
            AHardwareBuffer_release(NULL);
        }
    } else if (strcmp(name, "null-acquire") == 0) {
        AHardwareBuffer_acquire(NULL);
    } else if (strcmp(name, "non-null") == 0) {
        AHardwareBuffer_acquire(fake_ahb);
        AHardwareBuffer_release(fake_ahb);
    } else {
        fprintf(stderr, "unknown case: %s\n", name);
        return 2;
    }

    ahb_stub_counts(&acquire_calls, &release_calls);
    printf("SUMMARY real-acquire=%d real-release=%d\n",
           acquire_calls, release_calls);
    return 0;
}

#endif
