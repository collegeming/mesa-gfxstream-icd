#define ROUNDTRIP_CPU_ONLY
#include "guest_native_buffer_roundtrip.c"

#define REQUIRE(condition) do { if (!(condition)) { LOG("CPU_TEST FAIL line=%d", __LINE__); return 1; } } while (0)
int main(void) {
    uint32_t source[BUFFER_SIZE / sizeof(uint32_t)], destination[BUFFER_SIZE / sizeof(uint32_t)];
    for (uint32_t i = 0; i < BUFFER_SIZE / sizeof(uint32_t); ++i) {
        source[i] = UINT32_C(0xeeeeeeee); destination[i] = UINT32_C(0xdddddddd);
    }
    REQUIRE(prepare_payload(NULL, destination, BUFFER_SIZE) == 20);
    REQUIRE(prepare_payload(source, NULL, BUFFER_SIZE) == 20);
    REQUIRE(prepare_payload(source, destination, BUFFER_SIZE - 1u) == 20);
    REQUIRE(prepare_payload(source, destination, BUFFER_SIZE + 1u) == 20);
    REQUIRE(prepare_payload((unsigned char *)source + 1, destination, BUFFER_SIZE) == 20);
    REQUIRE(prepare_payload(source, (unsigned char *)destination + 1, BUFFER_SIZE) == 20);
    REQUIRE(prepare_payload(source, source, BUFFER_SIZE) == 20);
    REQUIRE(prepare_payload(source, source + 1, BUFFER_SIZE) == 20);
    REQUIRE(prepare_payload(source + 1, source, BUFFER_SIZE) == 20);
    REQUIRE(!payload_bounds((const void *)(UINTPTR_MAX - 3u), BUFFER_SIZE));
    REQUIRE(verify_payload(NULL, BUFFER_SIZE) == 20);
    REQUIRE(verify_payload(destination, 0) == 20);
    for (uint32_t i = 0; i < BUFFER_SIZE / sizeof(uint32_t); ++i) {
        REQUIRE(source[i] == UINT32_C(0xeeeeeeee)); REQUIRE(destination[i] == UINT32_C(0xdddddddd));
    }
    REQUIRE(prepare_payload(source, destination, BUFFER_SIZE) == 0);
    for (uint32_t i = 0; i < WORDS; ++i) {
        uint32_t expected = UINT32_C(0x13579bdf) ^ (UINT32_C(0x01020408) * (i + 1u));
        REQUIRE(source[i] == expected); REQUIRE(destination[i] == ~expected);
    }
    REQUIRE(verify_payload(destination, BUFFER_SIZE) == 21); /* A no-op copy MUST fail. */
    for (uint32_t i = 0; i < WORDS; ++i) destination[i] = source[i]; /* CPU oracle only. */
    REQUIRE(verify_payload(destination, BUFFER_SIZE) == 0);
    for (uint32_t i = 0; i < WORDS; ++i) {
        destination[i] ^= 1u;
        REQUIRE(verify_payload(destination, BUFFER_SIZE) == 21);
        destination[i] ^= 1u;
    }
    for (uint32_t i = WORDS; i < BUFFER_SIZE / sizeof(uint32_t); ++i) {
        REQUIRE(source[i] == UINT32_C(0xeeeeeeee)); REQUIRE(destination[i] == UINT32_C(0xdddddddd));
    }
    LOG("CPU_TEST PASS production helpers; invalid inputs untouched; no-op rejected; each of64 corruptions rejected; tail canaries intact; ordinary arrays only, NO Vulkan");
    return 0;
}
