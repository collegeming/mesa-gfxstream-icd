#pragma once
#include <cstdint>

namespace gfxstream::host::vk {
// Cache maintenance for memory the host imported with VK_EXT_external_memory_host.
//
// These pages belong to the guest. The host never called vkMapMemory on them, so
// vkFlushMappedMemoryRanges/vkInvalidateMappedMemoryRanges are not legal here (they require a
// mapping made by this VkDevice) and the CPU cache has to be driven directly. On ARM64 the guest
// and the host address the same physical pages and the caches are physically indexed, so
// maintenance performed here is also what the guest's own view needs.
//
// Two operations, matching the two halves of the coherent contract:
//   clean      - dc cvac : write dirty lines back to the point of coherency, for data the guest
//                          CPU wrote and the GPU is about to read.
//   invalidate - dc civac: clean and then discard the line, for data the GPU wrote and the guest
//                          CPU is about to read. civac rather than ivac because a dirty line can
//                          still hold a guest write that has not reached memory.
namespace host_pointer_cache {

inline void maintain(void* base, uint64_t size, bool invalidate) {
    if (!base || size == 0) return;
#if defined(__aarch64__)
    // CTR_EL0.DminLine is log2(words per line). 64 bytes is the ARMv8 minimum and the value on
    // every core this ships on, but read it rather than assume it.
    uint64_t ctr = 0;
    __asm__ __volatile__("mrs %0, ctr_el0" : "=r"(ctr));
    const uintptr_t lineSize = static_cast<uintptr_t>(4u) << ((ctr >> 16) & 0xfu);
    // The range is rounded down to a line boundary: maintenance is per line, and the first byte
    // of the import need not be line aligned.
    const uintptr_t begin = reinterpret_cast<uintptr_t>(base) & ~(lineSize - 1);
    const uintptr_t end = reinterpret_cast<uintptr_t>(base) + static_cast<uintptr_t>(size);
    for (uintptr_t address = begin; address < end; address += lineSize) {
        if (invalidate) {
            __asm__ __volatile__("dc civac, %0" : : "r"(address) : "memory");
        } else {
            __asm__ __volatile__("dc cvac, %0" : : "r"(address) : "memory");
        }
    }
    // The operations are broadcast to the inner shareable domain; without the barrier a
    // subsequent read on this core could still be served by a line that is being invalidated.
    __asm__ __volatile__("dsb ish" : : : "memory");
#elif defined(__GNUC__) || defined(__clang__)
    // No data-cache maintenance to do on a host that keeps caches coherent with DMA, but the
    // compiler still has to be told the memory changed underneath it.
    (void)invalidate;
    char* begin = static_cast<char*>(base);
    __builtin___clear_cache(begin, begin + size);
#else
    (void)invalidate;
    (void)base;
    (void)size;
#endif
}

inline void clean(void* base, uint64_t size) { maintain(base, size, false); }
inline void invalidate(void* base, uint64_t size) { maintain(base, size, true); }

}  // namespace host_pointer_cache
}  // namespace gfxstream::host::vk
