#!/usr/bin/env python3
"""Single-variable behavior fix for crosvm849e transfer_read format selection.

The existing code treats a cached renderer query result of drm_fourcc=0 as a
usable source format.  0 has no pixel layout, so convert_scanout_row() refuses
and the CPU edge falls back to a raw copy; the ABGR8888 sink then shows the
BGRA producer bytes with R and B exchanged.

This patch inserts one predicate: a zero fourcc is "no answer", so the already
present scanout_data fallback (AR24/ARGB8888, BlueFirst) is used instead.
No sink format, alpha handling, transfer path or renderer call is changed.

Apply AFTER patch_crosvm_scanout_contract.py (metadata variant) or directly on
the exact 849e baseline; the anchor below is identical in both.
"""
import argparse
import hashlib
from pathlib import Path

TARGET = "devices/src/virtio/gpu/virtio_gpu.rs"
OLD = """            .map(|query| query.drm_fourcc)
            .or_else(|| resource.scanout_data.map(|data| data.drm_format.0))
"""
NEW = """            .map(|query| query.drm_fourcc)
            .filter(|fourcc| *fourcc != 0)
            .or_else(|| resource.scanout_data.map(|data| data.drm_format.0))
"""


def transform(text):
    if text.count(OLD) != 1:
        raise ValueError("source anchor count mismatch: .map(drm_fourcc)/.or_else(scanout_data)")
    out = text.replace(OLD, NEW)
    # The fix must not add or remove any renderer/window/pixel operation.
    for operation in (".query(", "transfer_read(", "copy_from_frame(",
                      "can_copy_direct_from(", "DRM_FORMAT_ABGR8888,"):
        assert text.count(operation) == out.count(operation), operation
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--check", action="store_true", help="validate anchor; do not write")
    args = parser.parse_args()
    path = args.root / TARGET
    old = path.read_text(encoding="utf-8")
    new = transform(old)
    if not args.check:
        path.write_text(new, encoding="utf-8", newline="\n")
    print(hashlib.sha256(new.encode()).hexdigest() + "  " + TARGET)
    print("CHECK_ONLY" if args.check else
          "APPLIED_FIX_FOURCC_ZERO_FALLBACK; BEHAVIOR_CHANGE; NOT_BUILT; NOT_DEVICE_TESTED")


if __name__ == "__main__":
    main()
