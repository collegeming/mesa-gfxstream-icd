#define MAP_IDENTITY_CPU_ONLY
#include "guest_native_map_identity.c"
#include <assert.h>

int main(void) {
    const uint64_t present = UINT64_C(1) << 63, swapped = UINT64_C(1) << 62;
    const uint64_t mask = (UINT64_C(1) << 55) - 1u;
    struct page_identity p = decode_pagemap(present | UINT64_C(0x123456));
    assert(p.present == 1 && p.swapped == 0 && p.pfn == UINT64_C(0x123456));
    p = decode_pagemap(swapped | 7u);
    assert(p.present == 0 && p.swapped == 1 && p.pfn == 7u);
    p = decode_pagemap(0);
    assert(!p.present && !p.swapped && !p.pfn);
    p = decode_pagemap(present);
    assert(p.present && !p.swapped && !p.pfn); /* Redacted/unknown, not physical zero proof. */
    p = decode_pagemap(UINT64_MAX);
    assert(p.present && p.swapped && p.pfn == mask);
    for (unsigned bit = 0; bit < 64; ++bit) {
        p = decode_pagemap(UINT64_C(1) << bit);
        assert(p.present == (bit == 63));
        assert(p.swapped == (bit == 62));
        assert(p.pfn == (bit < 55 ? UINT64_C(1) << bit : 0));
    }
    off_t a = -1, b = -1;
    assert(!pagemap_offset(0x12345000u, 4096u, &a));
    assert(!pagemap_offset(0x12345ffcu, 4096u, &b));
    assert(a == (off_t)(UINT64_C(0x12345) * 8u) && a == b);
    assert(!pagemap_offset(0x12346000u, 4096u, &b) && b == a + 8);
    assert(pagemap_offset(0, 0, &a));
    assert(pagemap_offset(0, 4095, &a));
    assert(pagemap_offset(UINTPTR_MAX, 1, &a));
    assert(!pagemap_offset(UINTPTR_MAX, 4096, &a));
    uint32_t payload[REQUESTED_SIZE / sizeof(uint32_t)] = {0};
    assert(fault_and_mark(NULL, REQUESTED_SIZE) == 20);
    assert(fault_and_mark(payload, 0) == 20);
    assert(fault_and_mark(payload, REQUESTED_SIZE - 1u) == 20);
    assert(fault_and_mark(payload, REQUESTED_SIZE + 1u) == 20);
    assert(fault_and_mark((unsigned char *)payload + 1, REQUESTED_SIZE) == 20);
    assert(fault_and_mark((void *)(UINTPTR_MAX - 3u), REQUESTED_SIZE) == 20);
    assert(!fault_and_mark(payload, sizeof(payload)));
    assert(payload[0] == UINT32_C(0x13579bdf));
    assert(payload[1023] == UINT32_C(0x2468ace0));
    for (unsigned i = 1; i < 1023; ++i) assert(payload[i] == 0);
    puts("PASS actual production bit decode (all 64 bits), same-page offsets, overflow and payload bounds; CPU only");
    return 0;
}
