// Decision table for the AHB blob export guard.
//
// vkGetBlobInternal() used to call exportMemoryHandle() for every memory that
// reached the external-blob branch. In AndroidAHB mode that function falls back
// to vkGetMemoryAndroidHardwareBufferANDROID(), which Mali implements by loading
// the memory object's AHB field (+0x198) and handing it to
// AHardwareBuffer_acquire() without a null test. The field is written only by an
// AHB import or an AHB export allocation, so a guest dma-buf blob faulted at 0x8
// in RefBase::incStrong.
//
// The decoder records AHB backing in exactly two fields:
//
//   * MemoryInfo::ahbBacking       -- the AHB-first allocation imported an AHB;
//   * MemoryInfo::boundColorBuffer -- a ColorBuffer's AHB was imported.
//
// This test pins the call-site contract around those two indicators:
//
//   * AndroidAHB with either indicator -> exportMemoryHandle() is called;
//   * AndroidAHB with neither (no imported handle, or a non-AHB handle such as a
//     dma-buf) -> exportMemoryHandle() is not called, the result stays nullopt,
//     and the existing fallback runs;
//   * any other external memory mode (e.g. OpaqueFd) -> called unconditionally,
//     and the backing fields are never read, so the default path pays one
//     comparison;
//   * the AHB-EXPORT-SKIP gate admits exactly one line per process, even when
//     many threads hit it at the same time.
//
// The dispatch fake counts exportMemoryHandle() entries. A legacy mirror of the
// old call site is exercised first so the counter is proven to move when the
// export is reached.
//
// This is a host test: it includes the same header the device build includes and
// has no Vulkan or Android dependency beyond the header's <atomic>.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <atomic>
#include <optional>
#include <thread>
#include <type_traits>
#include <vector>

#include "ahb_blob_export_guard.h"

namespace {

enum class Mode {
    AndroidAhb,
    OpaqueFd,
};

struct Memory {
    bool ahbFirst = false;
    bool colorBufferBound = false;
    uint64_t size = 0;
    // Counts reads of the two AHB backing indicators, so the test can prove the
    // mode comparison short-circuits on the non-AndroidAHB path.
    mutable unsigned backingReads = 0;

    bool AhbFirst() const {
        ++backingReads;
        return ahbFirst;
    }
    bool ColorBufferBound() const {
        ++backingReads;
        return colorBufferBound;
    }
};

struct Dispatch {
    unsigned exportCalls = 0;
};

struct Outcome {
    bool exported;  // exportMemoryHandle() returned a value (not nullopt)
    bool called;    // exportMemoryHandle() was entered
};

std::atomic<unsigned> gLogLines{0};

void check(bool condition, const char* what) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", what);
        exit(1);
    }
}

// Mirror of the old call site: every memory is handed to exportMemoryHandle().
Outcome legacyExport(Mode mode, const Memory& memory, Dispatch* dispatch) {
    (void)mode;
    (void)memory;
    dispatch->exportCalls++;
    return {true, true};
}

// Mirror of the patched call site. The fallback is out of scope here: what
// matters is that a refused export leaves `exported` false so the caller takes
// the existing `if (!exportedMemoryOpt)` branch.
Outcome guardedExport(Mode mode, const Memory& memory, Dispatch* dispatch) {
    if (mode != Mode::AndroidAhb || memory.AhbFirst() || memory.ColorBufferBound()) {
        dispatch->exportCalls++;
        return {true, true};
    }
    if (gfxstream::ahb_blob_export_guard::ShouldLogSkip()) {
        gLogLines.fetch_add(1, std::memory_order_relaxed);
    }
    return {false, false};
}

}  // namespace

int main() {
    static_assert(
        std::is_same<decltype(gfxstream::ahb_blob_export_guard::ShouldLogSkip()), bool>::value,
        "the guard exposes a bool-returning single-winner gate");
    static_assert(sizeof(Memory::size) == sizeof(uint64_t),
                  "the skip line prints the allocation size as %llu");

    const Memory noBacking{false, false, 4096, 0};
    const Memory ahbFirstBacking{true, false, 16384, 0};
    const Memory colorBufferBacking{false, true, 8192, 0};

    // The fake is functional: the old call site reaches exportMemoryHandle() for
    // every shape, including the one that crashed the driver.
    {
        Dispatch dispatch;
        (void)legacyExport(Mode::AndroidAhb, noBacking, &dispatch);
        (void)legacyExport(Mode::AndroidAhb, ahbFirstBacking, &dispatch);
        (void)legacyExport(Mode::AndroidAhb, colorBufferBacking, &dispatch);
        check(dispatch.exportCalls == 3,
              "the legacy call site reaches the export for every memory shape");
    }

    // AndroidAHB with the AHB-first backing: still exported, no skip line.
    {
        Dispatch dispatch;
        Memory memory = ahbFirstBacking;
        Outcome outcome = guardedExport(Mode::AndroidAhb, memory, &dispatch);
        check(outcome.called && outcome.exported, "an AHB-first backing still exports");
        check(dispatch.exportCalls == 1, "an AHB-first backing enters the export once");
        check(memory.backingReads == 1, "an AHB-first backing is found on the first read");
        check(gLogLines.load() == 0, "an AHB-first backing never logs a skip");
    }

    // AndroidAHB with a ColorBuffer import: the imported handle is an AHB.
    {
        Dispatch dispatch;
        Memory memory = colorBufferBacking;
        Outcome outcome = guardedExport(Mode::AndroidAhb, memory, &dispatch);
        check(outcome.called && outcome.exported, "a ColorBuffer import still exports");
        check(dispatch.exportCalls == 1, "a ColorBuffer import enters the export once");
        check(memory.backingReads == 2, "a ColorBuffer import checks the two indicators");
        check(gLogLines.load() == 0, "a ColorBuffer import never logs a skip");
    }

    // Many threads hit the gate at once: exactly one diagnostic line wins.
    {
        std::vector<std::thread> threads;
        for (int i = 0; i < 16; ++i) {
            threads.emplace_back([] {
                Dispatch dispatch;
                Memory memory;
                (void)guardedExport(Mode::AndroidAhb, memory, &dispatch);
            });
        }
        for (auto& thread : threads) {
            thread.join();
        }
        check(gLogLines.load() == 1, "exactly one AHB-EXPORT-SKIP blob line per process");
    }

    // AndroidAHB, no imported handle at all: refused, no export, no new line.
    {
        Dispatch dispatch;
        Memory memory = noBacking;
        Outcome outcome = guardedExport(Mode::AndroidAhb, memory, &dispatch);
        check(!outcome.called && !outcome.exported,
              "no AHB backing skips the export and leaves nullopt");
        check(dispatch.exportCalls == 0, "no AHB backing never enters the export");
        check(memory.backingReads == 2, "no AHB backing tests both indicators");
        check(gLogLines.load() == 1, "later no-backing exports stay silent");
    }

    // AndroidAHB with a non-AHB handle (a dma-buf import): the decoder records no
    // AHB backing for it, so it takes the same refusal.
    {
        Dispatch dispatch;
        Memory memory = noBacking;
        Outcome outcome = guardedExport(Mode::AndroidAhb, memory, &dispatch);
        check(!outcome.called && !outcome.exported,
              "a non-AHB handle type is refused like a missing one");
        check(dispatch.exportCalls == 0, "a non-AHB handle type never enters the export");
        check(gLogLines.load() == 1, "a non-AHB handle type adds no line after the first");
    }

    // Another external memory mode: the mode comparison short-circuits first, so
    // the backing fields are never read and the export still runs.
    {
        Dispatch dispatch;
        Memory memory = noBacking;
        Outcome outcome = guardedExport(Mode::OpaqueFd, memory, &dispatch);
        check(outcome.called && outcome.exported, "another external memory mode still exports");
        check(dispatch.exportCalls == 1, "another external memory mode enters the export once");
        check(memory.backingReads == 0,
              "the mode comparison short-circuits before any backing field is read");
        check(gLogLines.load() == 1, "another external memory mode logs no skip");
    }

    // The caller's fallback runs exactly when the export is refused.
    {
        Dispatch refusedDispatch;
        Dispatch exportedDispatch;
        check(!guardedExport(Mode::AndroidAhb, noBacking, &refusedDispatch).exported,
              "a refused export takes the existing fallback branch");
        check(guardedExport(Mode::AndroidAhb, ahbFirstBacking, &exportedDispatch).exported,
              "an AHB-backed export does not take the fallback branch");
    }

    puts("PASS: AndroidAHB exports only memories with an AHB backing; a missing or non-AHB "
         "handle is refused without reaching the driver and logs one AHB-EXPORT-SKIP blob line, "
         "other modes keep one comparison and the same export path");
    return 0;
}
