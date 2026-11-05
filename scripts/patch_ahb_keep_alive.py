#!/usr/bin/env python3
"""Install the ColorBuffer AHB keep-alive switch.

With GFXSTREAM_AHB_KEEP_ALIVE=1 the PLATFORM_AHB branch of
freeExternalMemoryLocked() skips AHardwareBuffer_release() and writes a
rate-limited AHB-KEEP-ALIVE line. Without the environment variable the release
runs exactly as before. No other release point and no import path is touched.

The script is idempotent: every insertion carries a marker and a second run
verifies the marked block instead of patching again.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="Patched gfxstream source checkout")
root = parser.parse_args().root.resolve()

HEADER_SOURCE = Path(__file__).resolve().parent / "ahb_keep_alive.h"
HEADER_TARGET = root / "host/vulkan/ahb_keep_alive.h"
OPS = root / "host/vulkan/vk_common_operations.cpp"


def edit(path: Path, old: str, new: str, marker: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        if text.count(new) != 1:
            raise SystemExit(f"{path.name}: {marker} present but the marked block is not intact")
        print(f"already patched {path.name} ({marker})")
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path.name}: expected one {marker} anchor, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {path.name} ({marker})")


header_text = HEADER_SOURCE.read_text(encoding="utf-8")
for needle in ("GFXSTREAM_AHB_KEEP_ALIVE", "AHB-KEEP-ALIVE ptr=%p tid=%d",
               "const char* value = getenv(", "kLogLimit = 16"):
    assert needle in header_text, f"keep-alive header is missing {needle}"
if HEADER_TARGET.is_file():
    if HEADER_TARGET.read_text(encoding="utf-8") != header_text:
        raise SystemExit(f"{HEADER_TARGET}: exists but does not match the keep-alive header")
    print("already copied host/vulkan/ahb_keep_alive.h")
else:
    HEADER_TARGET.write_text(header_text, encoding="utf-8")
    print("copied host/vulkan/ahb_keep_alive.h")

# The include sits next to the crash tracer include in the only TU that owns
# freeExternalMemoryLocked().
INCLUDE_OLD = """// AHB-CRASH-TRACE-INCLUDE-END
"""
INCLUDE_NEW = """// AHB-CRASH-TRACE-INCLUDE-END

// AHB-KEEP-ALIVE-INCLUDE-BEGIN
#include "ahb_keep_alive.h"
// AHB-KEEP-ALIVE-INCLUDE-END
"""
edit(OPS, INCLUDE_OLD, INCLUDE_NEW, "AHB-KEEP-ALIVE-INCLUDE-BEGIN")

# The release is the last statement of the crash-trace free-pre block. Wrap
# only this call: the free-post crumb still fires either way, so the ring shows
# whether the release was skipped.
RELEASE_OLD = """                    // AHB-CRASH-TRACE-FREE-PRE-END
                    AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                        reinterpret_cast<void*>(info->handleInfo->handle)));
"""
RELEASE_NEW = """                    // AHB-CRASH-TRACE-FREE-PRE-END
                    // AHB-KEEP-ALIVE-RELEASE-BEGIN
                    if (::gfxstream::ahb_keep_alive::Enabled()) {
                        ::gfxstream::ahb_keep_alive::LogSkip(
                            reinterpret_cast<void*>(info->handleInfo->handle));
                    } else {
                        AHardwareBuffer_release(static_cast<AHardwareBuffer*>(
                            reinterpret_cast<void*>(info->handleInfo->handle)));
                    }
                    // AHB-KEEP-ALIVE-RELEASE-END
"""
edit(OPS, RELEASE_OLD, RELEASE_NEW, "AHB-KEEP-ALIVE-RELEASE-BEGIN")

print("AHB keep-alive switch installed (default off; GFXSTREAM_AHB_KEEP_ALIVE=1 enables)")
