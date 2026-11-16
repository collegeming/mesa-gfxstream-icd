#!/usr/bin/env python3
"""Disable crosvm's host soong variant for a target-only candidate build.

`m crosvm` builds both variants because Android.bp declares host_supported: true. The host
variant pulls hardware/google/gfxstream host/vulkan/decoder_profile.cpp, which fails on the
CI glibc toolchain with "use of undeclared identifier 'gettid'". The shipped artifact is the
Android arm64 target binary, so build only that variant.

This is a build-system edit, not part of the behavior change: it is recorded in the same
source.patch and host-variant-disabled.txt evidence as the MemoryMappingArena patch.

Run with -I.
"""
import argparse
from pathlib import Path

OLD = (
    '    name: "crosvm",\n'
    '    defaults: ["crosvm_defaults"],\n'
    '    host_supported: true,\n'
)
NEW = (
    '    name: "crosvm",\n'
    '    defaults: ["crosvm_defaults"],\n'
    '    host_supported: false,\n'
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="external/crosvm tree")
    args = parser.parse_args()
    path = args.root / "Android.bp"
    text = path.read_text(encoding="utf-8")
    if text.count(OLD) != 1:
        raise RuntimeError(f"expected exactly one crosvm module anchor, found {text.count(OLD)}")
    path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print("crosvm host variant disabled; target aarch64 is the candidate")


if __name__ == "__main__":
    main()
