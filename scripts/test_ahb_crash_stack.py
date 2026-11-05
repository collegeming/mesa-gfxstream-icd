#!/usr/bin/env python3
"""Static checks for the diagnostic AHB crash stack tracer.

Usage: test_ahb_crash_stack.py <patched-gfxstream-root> [workflow.yml]

The patch has already been applied by the workflow at this point. The test
verifies the shipped header, the exact breadcrumb insertion points, that the
tracer is default-off, and that a second patch run is a byte-for-byte no-op.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
workflow = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
here = Path(__file__).resolve().parent

header = root / "host/vulkan/ahb_crash_trace.h"
ops = root / "host/vulkan/vk_common_operations.cpp"
decoder = root / "host/vulkan/vk_decoder_global_state.cpp"


def read(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"{path}: missing")
    return path.read_text(encoding="utf-8")


header_text = read(header)
assert header_text == (here / "ahb_crash_trace.h").read_text(encoding="utf-8"), \
    "host/vulkan/ahb_crash_trace.h differs from the CI copy"
print("PASS: host/vulkan/ahb_crash_trace.h matches the CI copy")

for needle in ("GFXSTREAM_CRASH_TRACE", "GFXSTREAM_CRASH_TRACE_FILE",
               "GFXSTREAM_CRASH_MAPS_PATH", "_Unwind_Backtrace", "sigaction",
               "SA_SIGINFO", "prev", "AHB-CRASH-TRACE"):
    assert needle in header_text, f"header missing {needle}"
assert "v[0] == '1' && v[1] == '\\0'" in header_text, \
    "enable gate is not the exact GFXSTREAM_CRASH_TRACE=1 check"
assert 'extern "C" inline void GfxAhbCrashTraceHandler' in header_text, \
    "the signal handler must be inline so both translation units share one definition"
assert "printf(" not in header_text and "malloc(" not in header_text, \
    "signal path must not use printf/malloc"
print("PASS: header is default-off, async-signal-safe, and chains to the previous handler")

for marker in ("AHB-CRASH-TRACE-INCLUDE-BEGIN", "AHB-CRASH-TRACE-ALLOC-BEGIN",
               "AHB-CRASH-TRACE-IMPORT-CB-BEGIN", "AHB-CRASH-TRACE-ALIAS-BEGIN"):
    assert ops.read_text(encoding="utf-8").count(marker) == 1, \
        f"{marker} must appear exactly once in vk_common_operations.cpp"
assert ops.read_text(encoding="utf-8").count('#include "ahb_crash_trace.h"') == 1
print("PASS: vk_common_operations.cpp include and the deployed AHB sites are marked exactly once")

decoder_text = decoder.read_text(encoding="utf-8")
assert decoder_text.count('#include "ahb_crash_trace.h"') == 1
assert decoder_text.count("AHB-CRASH-TRACE-INCLUDE-DECODER-BEGIN") == 1
for marker in ("AHB-CRASH-TRACE-DECODER-HANDLE-BEGIN", "AHB-CRASH-TRACE-GUEST-AHB-BEGIN",
               "AHB-CRASH-TRACE-FIRST-BEGIN"):
    assert decoder_text.count(marker) == 1, \
        f"{marker} must appear exactly once in vk_decoder_global_state.cpp"
print("PASS: decoder includes the tracer; handle import, guest pNext AHB import, and AHB-first helper are traced")

# Re-running the patch must not change one byte.
before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (header, ops, decoder)}
subprocess.run([sys.executable, "-I", str(here / "patch_ahb_crash_stack.py"), str(root)],
               check=True, stdout=subprocess.PIPE, text=True)
after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (header, ops, decoder)}
assert before == after, "second patch run modified the tree"
print("PASS: patch is idempotent (second run is a byte-for-byte no-op)")

if workflow is not None:
    workflow_text = read(workflow)
    assert "diagnostic/ahb-crash-stack" in workflow_text, "workflow does not list the branch"
    assert "scripts/*ahb_crash_stack*" in workflow_text, "workflow does not watch the scripts"
    assert "python3 -I ci/scripts/patch_ahb_crash_stack.py source" in workflow_text
    assert "test_ahb_crash_stack.cpp" in workflow_text
    print("PASS: workflow applies and tests the tracer on the diagnostic branch")

print("ALL AHB-CRASH-TRACE STATIC TESTS PASSED")
