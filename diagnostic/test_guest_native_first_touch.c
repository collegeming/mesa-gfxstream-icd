/* Actions-only: ordinary, initialized CPU array; no Vulkan header or linkage. */
#define FIRST_TOUCH_CPU_ONLY
#include "guest_native_first_touch.c"
#include <assert.h>
#include <limits.h>

int main(void) {
    _Alignas(uint32_t) uint32_t words[1026];
    const uint32_t sentinel = UINT32_C(0xa5a5a5a5);
    for (size_t i = 0; i < 1026; ++i) words[i] = sentinel;
    void *base = &words[1];
    assert(cpu_first_touch(NULL, 4096) == 20);
    assert(cpu_first_touch(base, 0) == 20);
    assert(cpu_first_touch(base, 4095) == 20);
    assert(cpu_first_touch(base, 4097) == 20);
    assert(cpu_first_touch(base, SIZE_MAX) == 20);
    assert(cpu_first_touch((unsigned char *)base + 1, 4096) == 20);
    uintptr_t near_end = UINTPTR_MAX - (UINTPTR_MAX % _Alignof(uint32_t));
    assert(cpu_first_touch((void *)near_end, 4096) == 20);
    for (size_t i = 0; i < 1026; ++i) assert(words[i] == sentinel);
    assert(cpu_first_touch(base, 4096) == 0);
    assert(words[1] == UINT32_C(0x13579bdf));
    assert(words[1024] == UINT32_C(0x2468ace0));
    assert(words[0] == sentinel && words[1025] == sentinel);
    for (size_t i = 2; i < 1024; ++i) assert(words[i] == sentinel);
    puts("PASS actual production scalar helper: bounds/alignment/overflow rejection, two markers, untouched interior and guard words; CPU ordinary array only");
    return 0;
}
