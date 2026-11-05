#!/usr/bin/env python3
"""Runtime file switch for the explicit ColorBuffer AHardwareBuffer_allocate path.

Why
---
The previous switch was an environment variable read at the first allocation. That
cannot answer the A/B question on a running VM: the environment is fixed when
crosvm starts, and gating allocAhb() from process start also changes the startup
allocation path that already calls it, so the VM dies before the probe can run.

What
----
The switch is now the existence of a regular file, checked with stat() on every
call to allocAhb() in host/vulkan/vk_common_operations.cpp:

    /data/local/tmp/gfxstream-ahb-off

GFXSTREAM_AHB_ALLOC_OFF_FILE overrides only the path; it never enables or
disables the switch. With the file absent, stat() fails (ENOENT/EACCES/ENOTDIR,
all treated as "not disabled") and the rest of allocAhb() is byte-identical to
the pinned source. With the file present at the moment of a call, the function
logs exactly one line and returns nullptr before AHardwareBuffer_allocate():

    GFXSTREAM_AHB_ALLOC_DISABLED file=%s

The caller already handles nullptr: allocExternalMemory() logs "Falling back to
non-exportable allocation" and breaks to the ordinary allocation path.

The check is deliberately not cached: creating the file after crosvm is already
running takes effect on the next explicit ColorBuffer allocation, and removing
it restores the original path immediately, in the same process. stat() is a
read-only syscall and the helper holds no mutable state, so concurrent
allocations cannot observe a half-updated decision.

The path lookup is a function-local static, resolved on the first allocation,
never from a static initializer at library load. The startup/first-allocation
path, allocateAhbFirstMemory() in vk_decoder_global_state.cpp, is left
byte-identical and does not include the helper header: it is not an explicit
AHardwareBuffer_allocate request. If startup does reach allocAhb(), the file is
absent during boot, so its behavior is unchanged as well.

The helper lives in host/vulkan/ahb_alloc_off_file.h, created by this script.
The script is idempotent: a second run verifies the header, the include and the
guarded block instead of patching again. Every replacement requires exactly one
occurrence of the pinned text; all checks run before any file is written, so a
rejected patch leaves the tree untouched.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Pinned gfxstream source checkout")
root = parser.parse_args().root.resolve()

ops = "host/vulkan/vk_common_operations.cpp"
decoder = "host/vulkan/vk_decoder_global_state.cpp"
header = "host/vulkan/ahb_alloc_off_file.h"

ops_path = root / ops
decoder_path = root / decoder
header_path = root / header

for required in (ops_path, decoder_path):
    if not required.is_file():
        raise SystemExit(f"{required.relative_to(root)}: file not found under {root}")

HEADER = """\
// Runtime file switch for the explicit ColorBuffer AHardwareBuffer_allocate path.
//
// The switch is the existence of a regular file at the path below. It is checked on
// every allocation, never cached, so creating or removing the file takes effect in a
// running crosvm. GFXSTREAM_AHB_ALLOC_OFF_FILE relocates the path only; the file
// decides. Any stat() failure means "not disabled": no exception, no log, no side
// effect. The helper holds no mutable state and is safe to call concurrently.
#ifndef GFXSTREAM_HOST_VULKAN_AHB_ALLOC_OFF_FILE_H_
#define GFXSTREAM_HOST_VULKAN_AHB_ALLOC_OFF_FILE_H_

#include <stdlib.h>
#include <sys/stat.h>

namespace gfxstream {
namespace ahb_alloc_killswitch {

// Resolved lazily on the first allocation, so no static initializer reads the
// environment when the library loads.
inline const char* OffFilePath() {
    static const char* const kPath = []() -> const char* {
        const char* fromEnv = getenv("GFXSTREAM_AHB_ALLOC_OFF_FILE");
        return (fromEnv != nullptr && fromEnv[0] != '\\0')
                   ? fromEnv
                   : "/data/local/tmp/gfxstream-ahb-off";
    }();
    return kPath;
}

// True only when `path` names an existing regular file at this instant. Every
// failure (null/empty path, ENOENT, EACCES, ENOTDIR) is "not disabled".
inline bool IsOff(const char* path) {
    if (path == nullptr || path[0] == '\\0') {
        return false;
    }
    struct stat st = {};
    return stat(path, &st) == 0 && S_ISREG(st.st_mode);
}

}  // namespace ahb_alloc_killswitch
}  // namespace gfxstream

#endif  // GFXSTREAM_HOST_VULKAN_AHB_ALLOC_OFF_FILE_H_
"""

INCLUDE = '#include "ahb_alloc_off_file.h"\n'
INCLUDE_ANCHOR = (
    "#ifdef __ANDROID__\n"
    "// Allocate an AHardwareBuffer matching the given image's format, extent and usage.\n"
)
INCLUDE_BLOCK = (
    "#ifdef __ANDROID__\n"
    + INCLUDE
    + "// Allocate an AHardwareBuffer matching the given image's format, extent and usage.\n"
)
BEGIN = "    // AHB-ALLOC-KILLSWITCH-1-BEGIN\n"
END = "    // AHB-ALLOC-KILLSWITCH-1-END\n"
GUARD = (
    BEGIN
    + """    // Runtime file switch for the explicit ColorBuffer AHardwareBuffer_allocate path
    // only. The existence of the file is re-checked on every call, so it can be
    // created or removed while crosvm is running. When stat() fails (missing,
    // unreadable, or not a regular file) this is a no-op and the allocation below is
    // unchanged; the startup AHB-first helper does not use this switch.
    const char* ahbOffFile = gfxstream::ahb_alloc_killswitch::OffFilePath();
    if (gfxstream::ahb_alloc_killswitch::IsOff(ahbOffFile)) {
        GFXSTREAM_WARNING("GFXSTREAM_AHB_ALLOC_DISABLED file=%s", ahbOffFile);
        return nullptr;
    }
"""
    + END
)
LOG_LINE = 'GFXSTREAM_AHB_ALLOC_DISABLED file=%s'
ANCHOR = "static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {\n"
OLD_ENV_GATE = 'getenv("GFXSTREAM_DISABLE_AHB_ALLOC")'
OLD_DECODER_MARKER = "AHB-ALLOC-KILLSWITCH-2"

before = ops_path.read_text(encoding="utf-8")
decoder_text = decoder_path.read_text(encoding="utf-8")

# The old environment gate would make the A/B ambiguous and is not idempotent with
# the file switch: refuse it instead of layering two decisions.
if OLD_ENV_GATE in before or "static const bool disableAhbAlloc" in before:
    raise SystemExit(
        f"{ops}: old GFXSTREAM_DISABLE_AHB_ALLOC environment gate is still present; "
        "start from a clean pinned tree")
if OLD_DECODER_MARKER in decoder_text:
    raise SystemExit(
        f"{decoder}: old AHB-FIRST kill switch ({OLD_DECODER_MARKER}) is still present; "
        "start from a clean pinned tree so only the explicit allocAhb() path is switched")

# The helper header is created if missing and must match byte for byte if present.
existing_header = header_path.read_text(encoding="utf-8") if header_path.is_file() else None
if existing_header is not None and existing_header != HEADER:
    raise SystemExit(
        f"{header}: exists but does not match the file-switch helper; refusing to overwrite")

# Validate every ops anchor before writing anything.
text = before
if INCLUDE_BLOCK in text:
    if text.count(INCLUDE_BLOCK) != 1:
        raise SystemExit(f"{ops}: helper include block appears {text.count(INCLUDE_BLOCK)} times")
elif INCLUDE in text:
    raise SystemExit(
        f"{ops}: helper include exists outside the Android block; start from a clean tree")
else:
    if text.count(INCLUDE_ANCHOR) != 1:
        raise SystemExit(
            f"{ops}: expected one Android allocAhb() include anchor, "
            f"found {text.count(INCLUDE_ANCHOR)}")

if text.count(ANCHOR) != 1:
    raise SystemExit(f"{ops}: expected one allocAhb() anchor, found {text.count(ANCHOR)}")

if BEGIN in text or END in text:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"{ops}: kill switch markers are not paired")
    if text.count(GUARD) != 1:
        raise SystemExit(f"{ops}: kill switch marker present but the guarded block is not intact")

# Checks passed: write the helper first, then the include and the guard.
if existing_header is None:
    header_path.write_text(HEADER, encoding="utf-8", newline="\n")
    print("created", header)
else:
    print("already created", header)

if INCLUDE_BLOCK not in text:
    text = text.replace(INCLUDE_ANCHOR, INCLUDE_BLOCK, 1)
    print("patched", ops, "(helper include)")
else:
    print("already patched", ops, "(helper include)")

if BEGIN not in text:
    text = text.replace(ANCHOR, ANCHOR + GUARD, 1)
    print("patched", ops, "(AHB-ALLOC-KILLSWITCH-1)")
else:
    print("already patched", ops, "(AHB-ALLOC-KILLSWITCH-1)")

if text != before:
    ops_path.write_text(text, encoding="utf-8", newline="\n")

# The AHB-first/startup path is intentionally untouched.
for needle, label in (
    ("AHB-ALLOC-KILLSWITCH", "kill switch marker"),
    (OLD_ENV_GATE, "old environment gate"),
    ("GFXSTREAM_AHB_ALLOC_DISABLED", "kill switch log line"),
    (INCLUDE.strip(), "kill switch helper include"),
):
    if needle in decoder_text:
        raise SystemExit(
            f"{decoder}: {label} leaked into the startup path; "
            "allocateAhbFirstMemory() must stay byte-identical")

print("AHB allocation file switch installed (explicit allocAhb() path only)")
