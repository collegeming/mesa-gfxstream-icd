#!/usr/bin/env python3
"""Create 1-byte placeholders for include_bytes!() targets missing from crosvm.

The minimal AOSP manifest does not run every asset-generation step crosvm has in a full
platform build. The missing files are prebuilt/Gunyah assets (e.g.
hypervisor/src/gunyah/shim.bin); this candidate runs under KVM, so they are never executed,
but Rust's include_bytes!() needs the file to exist at compile time.

Only paths inside the given crosvm tree are touched, and only when missing.

Run with -I.
"""
import argparse
import re
from pathlib import Path

PATTERN = re.compile(r'include_bytes!\(\s*"([^"]+)"\s*\)')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="external/crosvm tree")
    args = parser.parse_args()
    root = args.root.resolve()
    created = []
    for rs in root.rglob("*.rs"):
        text = rs.read_text(errors="ignore")
        for match in PATTERN.finditer(text):
            target = (rs.parent / match.group(1)).resolve()
            if not target.exists() and root in target.parents:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x00")
                created.append(str(target.relative_to(root)))
    for path in sorted(set(created)):
        print(path)


if __name__ == "__main__":
    main()
