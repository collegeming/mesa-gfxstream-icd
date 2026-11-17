#!/usr/bin/env python3
"""CI-only fixed-tree preflight and deterministic artifact admission; no compilation."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

LOCK_SHA = "9eb91b9417aec42d0159e1edd986a9e59f0bd93b078ab06203a306b8df4581b0"
# Historical run43938110284 picked.txt:1, soong-crosvm.log:4100-4105.
OUTPUT = "out/soong/.intermediates/external/crosvm/crosvm_device_only/crosvm_device_only_bin/android_arm64_armv8-a/unstripped/crosvm"
MODULE = "//external/crosvm/crosvm_device_only:crosvm_device_only_bin"


def require(value, message):
    if not value:
        raise ValueError(message)


def read_lock(directory):
    raw = (directory / "resolved-manifest.xml").read_bytes()
    require(hashlib.sha256(raw).hexdigest() == LOCK_SHA, "fixed manifest SHA mismatch")
    root = ET.fromstring(raw)
    projects = root.findall("project")
    require(len(projects) == 97, "expected 97 fixed projects")
    require(len({p.get("path") for p in projects}) == 97, "duplicate fixed project")
    require(all(re.fullmatch(r"[0-9a-f]{40}", p.get("revision", "")) for p in projects),
            "unlocked project")
    return projects


def verify_tree(directory, tree, evidence):
    projects = read_lock(directory)
    records = []
    for project in projects:
        path, expected = project.get("path"), project.get("revision")
        actual = subprocess.check_output(["git", "-C", str(tree / path), "rev-parse", "HEAD"], text=True).strip()
        require(actual == expected, "project HEAD mismatch: " + path)
        dirty = subprocess.check_output(["git", "-C", str(tree / path), "status", "--porcelain"], text=True)
        require(not dirty, "project not pristine before paired patches: " + path)
        records.append({"path": path, "commit": actual})
    evidence.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")


def admit(tree, log):
    binary = tree / OUTPUT
    require(binary.is_file() and binary.stat().st_size > 0, "exact device-only output missing")
    with binary.open("rb") as stream:
        header = stream.read(20)
    require(header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\xb7\x00", "not ELF64 little-endian AArch64")
    text = log.read_text(errors="replace")
    require(MODULE + " rustc src/main.rs" in text and MODULE + " strip crosvm" in text,
            "current build lacks device-only rustc/link and strip evidence")
    require("build completed successfully" in text, "missing successful build completion")
    return {"output": OUTPUT, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "size": binary.stat().st_size, "machine": "AArch64", "device_status": "NOT_TESTED"}


def selfcheck(directory):
    from tempfile import TemporaryDirectory
    from unittest.mock import patch
    read_lock(directory)
    with TemporaryDirectory() as temporary:
        tree = Path(temporary)
        binary = tree / OUTPUT
        binary.parent.mkdir(parents=True)
        header = bytearray(64)
        header[:6], header[18:20] = b"\x7fELF\x02\x01", b"\xb7\x00"
        binary.write_bytes(header)
        log = tree / "build.log"
        log.write_text(MODULE + " rustc src/main.rs\n" + MODULE + " strip crosvm\nbuild completed successfully\n")
        assert admit(tree, log)["output"] == OUTPUT
        for bad in ("failed", MODULE + " rustc src/main.rs\nbuild completed successfully"):
            log.write_text(bad)
            try:
                admit(tree, log)
            except ValueError:
                pass
            else:
                raise AssertionError("failed/incomplete link accepted")
        evidence = tree / "must-not-publish.json"
        with patch.object(subprocess, "check_output", return_value="0" * 40):
            try:
                verify_tree(directory, tree, evidence)
            except ValueError:
                pass
            else:
                raise AssertionError("wrong project SHA accepted")
        assert not evidence.exists()
        (tree / "resolved-manifest.xml").write_text("<manifest/>")
        try:
            read_lock(tree)
        except ValueError:
            pass
        else:
            raise AssertionError("wrong lock accepted")
    print("fixed build dry preflight PASS; no tree sync/compiler/device executed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    read_lock(args.lock)
    if args.selfcheck:
        selfcheck(args.lock)
    elif args.log:
        require(args.tree and args.evidence and not args.evidence.exists(), "need tree/new evidence path")
        args.evidence.write_text(json.dumps(admit(args.tree, args.log), indent=2) + "\n", encoding="utf-8")
    else:
        require(args.tree and args.evidence and not args.evidence.exists(), "need tree/new evidence path")
        verify_tree(args.lock, args.tree, args.evidence)


if __name__ == "__main__":
    main()
