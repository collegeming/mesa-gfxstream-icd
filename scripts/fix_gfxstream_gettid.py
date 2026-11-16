#!/usr/bin/env python3
"""Make gfxstream's host/vulkan/decoder_profile.cpp compile against glibc.

The file calls gettid() without including <unistd.h>; bionic headers happen to declare it,
glibc does not, and the host-variant compile of libgfxstream_host_vulkan_server fails with
"use of undeclared identifier 'gettid'". Prepend the include, idempotently.

Build-time only; the behavior patch (MemoryMappingArena::remove) is independent.

Run with -I.
"""
import argparse
from pathlib import Path

REL = "hardware/google/gfxstream/host/vulkan/decoder_profile.cpp"
INCLUDE = "#include <unistd.h>\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="synced AOSP root")
    args = parser.parse_args()
    path = args.root / REL
    text = path.read_text(encoding="utf-8")
    if INCLUDE not in text:
        path.write_text(INCLUDE + text, encoding="utf-8")
        print(f"prepended {INCLUDE.strip()} to {REL}")
    else:
        print(f"{INCLUDE.strip()} already present in {REL}")


if __name__ == "__main__":
    main()
