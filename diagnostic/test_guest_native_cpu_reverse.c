/* Actions-only ordinary CPU array reverse-visibility oracle. Not a host implementation or Vulkan run. */
#define REVERSE_CPU_ONLY
#include "guest_native_cpu_reverse.c"
#include <assert.h>

static void check_guards(const uint32_t *words, uint32_t sentinel) {
    assert(words[0] == sentinel && words[1025] == sentinel);
    for (size_t i = 2; i < 1024; ++i) assert(words[i] == sentinel);
}

int main(void) {
    uint32_t words[1026];
    const uint32_t sentinel = UINT32_C(0xa5a5a5a5);
    for (size_t i = 0; i < 1026; ++i) words[i] = sentinel;
    void *base = &words[1];
    uintptr_t near_end = UINTPTR_MAX - (UINTPTR_MAX % _Alignof(uint32_t));
    for (unsigned phase = 0; phase < 2; ++phase) {
        bool reply = phase != 0;
        assert(cpu_reverse_words(NULL, 4096, reply) == 20);
        assert(cpu_reverse_words(base, 0, reply) == 20);
        assert(cpu_reverse_words(base, 4095, reply) == 20);
        assert(cpu_reverse_words(base, 4097, reply) == 20);
        assert(cpu_reverse_words(base, SIZE_MAX, reply) == 20);
        assert(cpu_reverse_words((unsigned char *)base + 1, 4096, reply) == 20);
        assert(cpu_reverse_words((void *)near_end, 4096, reply) == 20);
    }
    for (size_t i = 0; i < 1026; ++i) assert(words[i] == sentinel);
    assert(cpu_reverse_words(base, 4096, false) == 0);
    assert(words[1] == UINT32_C(0x13579bdf));
    assert(words[1024] == UINT32_C(0x2468ace0));
    check_guards(words, sentinel);
    /* Missing/incompatible host hook returning success must NOT pass unchanged requests.
     * Synthetic replies below do not depend on host request observation or matching. */
    assert(cpu_reverse_words(base, 4096, true) == 21);
    assert(words[1] == UINT32_C(0x13579bdf) && words[1024] == UINT32_C(0x2468ace0));
    words[1] = UINT32_C(0x89abcdef);
    assert(cpu_reverse_words(base, 4096, true) == 21); /* wrong last reply */
    words[1] = UINT32_C(0x13579bdf);
    words[1024] = UINT32_C(0xfedcba98);
    assert(cpu_reverse_words(base, 4096, true) == 21); /* wrong first reply */
    words[1] = UINT32_C(0x89abcdef);
    assert(cpu_reverse_words(base, 4096, true) == 0);
    assert(words[1] == UINT32_C(0x89abcdef) && words[1024] == UINT32_C(0xfedcba98));
    check_guards(words, sentinel);
    puts("PASS actual production reverse request-write/reply-verify helper: 14 bounds/alignment/overflow rejects, missing host and each wrong reply rejected, exact reply accepted, guards/interior untouched; ordinary CPU arrays only");
    return 0;
}
