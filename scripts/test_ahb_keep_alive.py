#!/usr/bin/env python3
"""Static checks for the ColorBuffer AHB keep-alive switch.

Usage: test_ahb_keep_alive.py <patched-gfxstream-root> [workflow.yml]

The checks pin the contract that makes the switch safe for a device run:
- default (unset) still calls AHardwareBuffer_release();
- only GFXSTREAM_AHB_KEEP_ALIVE=1 skips it;
- the environment is read on every call, never cached in a static initializer;
- the AHB-KEEP-ALIVE log is rate-limited;
- exactly one release site is wrapped, and the import path is untouched.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
workflow = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
here = Path(__file__).resolve().parent

header = root / "host/vulkan/ahb_keep_alive.h"
ops = root / "host/vulkan/vk_common_operations.cpp"


def read(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"{path}: missing")
    return path.read_text(encoding="utf-8")


header_text = read(header)
assert header_text == (here / "ahb_keep_alive.h").read_text(encoding="utf-8"), \
    "host/vulkan/ahb_keep_alive.h differs from the CI copy"
print("PASS: host/vulkan/ahb_keep_alive.h matches the CI copy")

assert 'getenv("GFXSTREAM_AHB_KEEP_ALIVE")' in header_text, \
    "the switch must read GFXSTREAM_AHB_KEEP_ALIVE"
assert 'value[0] == \'1\' && value[1] == \'\\0\'' in header_text, \
    "only the exact value 1 may enable the switch"
for line in header_text.splitlines():
    code = line.split("//", 1)[0]
    assert not ("static" in code and "getenv" in code), \
        "getenv() must not appear in a static declaration or initializer"
assert "const char* value = getenv(\"GFXSTREAM_AHB_KEEP_ALIVE\");" in header_text
print("PASS: the environment is read per call and never static-initialized")

assert '"AHB-KEEP-ALIVE ptr=%p tid=%d\\n"' in header_text, \
    "the log line must be exactly AHB-KEEP-ALIVE ptr=%p tid=%d"
assert "kLogLimit = 16" in header_text and "index >= kLogLimit" in header_text, \
    "the AHB-KEEP-ALIVE log must be rate-limited"
assert "logged.fetch_add" in header_text
print("PASS: the AHB-KEEP-ALIVE log is rate-limited to 16 lines per process")

ops_text = read(ops)
assert ops_text.count("AHB-KEEP-ALIVE-INCLUDE-BEGIN") == 1
assert ops_text.count("AHB-KEEP-ALIVE-RELEASE-BEGIN") == 1
assert ops_text.count('#include "ahb_keep_alive.h"') == 1
print("PASS: the switch is included and marked exactly once")

assert """if (::gfxstream::ahb_keep_alive::Enabled()) {
                        ::gfxstream::ahb_keep_alive::LogSkip(
                            reinterpret_cast<void*>(info->handleInfo->handle));
                    } else {
                        AHardwareBuffer_release(static_cast<AHardwareBuffer*>(""" in ops_text, \
    "the release must sit in the else branch, so the default path still releases"
assert "AHB-KEEP-ALIVE-RELEASE-END" in ops_text
print("PASS: default path keeps AHardwareBuffer_release(); env=1 takes the skip branch")

start = ops_text.index("AHB-KEEP-ALIVE-RELEASE-BEGIN")
end = ops_text.index("AHB-KEEP-ALIVE-RELEASE-END")
block = ops_text[start:end]
assert block.count("AHardwareBuffer_release(") == 1, \
    "the keep-alive block must wrap exactly one release"
release_calls = [line for line in ops_text.splitlines()
                 if "AHardwareBuffer_release(" in line.split("//", 1)[0]]
assert len(release_calls) == 4, \
    "the other three release sites must stay untouched"
assert "importAhbInfo.buffer = ahb;" in ops_text
print("PASS: only the freeExternalMemoryLocked release is wrapped; import path unchanged")

# Re-running the patch must not change one byte.
before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (header, ops)}
subprocess.run([sys.executable, "-I", str(here / "patch_ahb_keep_alive.py"), str(root)],
               check=True, stdout=subprocess.PIPE, text=True)
after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (header, ops)}
assert before == after, "second patch run modified the tree"
print("PASS: patch is idempotent (second run is a byte-for-byte no-op)")

if workflow is not None:
    workflow_text = read(workflow)
    assert "diagnostic/ahb-crash-stack" in workflow_text
    assert "scripts/*ahb_keep_alive*" in workflow_text, "workflow does not watch the scripts"
    assert "python3 -I ci/scripts/patch_ahb_keep_alive.py source" in workflow_text
    assert "test_ahb_keep_alive.cpp" in workflow_text
    print("PASS: workflow applies and tests the keep-alive switch")

print("ALL AHB-KEEP-ALIVE STATIC TESTS PASSED")
