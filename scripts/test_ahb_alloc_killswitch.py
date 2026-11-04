#!/usr/bin/env python3
"""Tests for the runtime AHB allocation kill switch and the patched source.

Checks, in this order:

1. Production invariants:
   * exactly one AHB-ALLOC-KILLSWITCH-1 in vk_common_operations.cpp, inside
     allocAhb() and before the single AHardwareBuffer_allocate() call;
   * exactly one AHB-ALLOC-KILLSWITCH-2 in vk_decoder_global_state.cpp, inside
     allocateAhbFirstMemory() and before its AHardwareBuffer_allocate() call;
   * the switch reads GFXSTREAM_DISABLE_AHB_ALLOC and returns nullptr first, so
     the unset/default path still reaches AHardwareBuffer_allocate;
   * the AHB-ALLOC log line is present at both sites;
   * allocExternalMemory() keeps the `if (!ahb)` non-exportable fallback, the
     AHB-first branch keeps AHB-FIRST-FALLBACK, and the deferred image-bind path
     still turns an on_vkCreateImage failure into an error return.
2. The patch script on a minimal fixture: a fresh run applies exactly once, a
   second run is a no-op, a missing anchor and a duplicated anchor are rejected
   with no file modified.
3. Workflow wiring: the paths filter, the artifact copy, and the patch/test
   order (after patch_ahb_first_hostvisible.py, which creates the second site).

Usage:
    python scripts/test_ahb_alloc_killswitch.py <gfxstream-root> [workflow]
"""
import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / "patch_ahb_alloc_killswitch.py"

ENV_NAME = 'GFXSTREAM_DISABLE_AHB_ALLOC'
LOG_LINE = 'GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx'
GUARD = f'if (getenv("{ENV_NAME}") != nullptr)'
ALLOC = 'AHardwareBuffer_allocate(&desc, &ahb)'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="gfxstream source root")
parser.add_argument("workflow", type=Path, nargs="?", help="CI workflow file")
args = parser.parse_args()
root = args.root.resolve()

ops = (root / "host/vulkan/vk_common_operations.cpp").read_text(encoding="utf-8")
decoder = (root / "host/vulkan/vk_decoder_global_state.cpp").read_text(encoding="utf-8")


def one(text, needle, label):
    count = text.count(needle)
    assert count == 1, f"{label}: expected 1 occurrence of {needle!r}, found {count}"


def after(text, first, second, label):
    a = text.index(first)
    b = text.index(second)
    assert a < b, f"{label}: {first!r} must precede {second!r}"
    return True


# ---------------------------------------------------------------------------
# 1. ColorBuffer allocation: allocAhb() returns before AHardwareBuffer_allocate.
# ---------------------------------------------------------------------------
one(ops, "AHB-ALLOC-KILLSWITCH-1", "ColorBuffer kill switch")
one(ops, ALLOC, "ColorBuffer AHardwareBuffer_allocate")
one(ops, LOG_LINE, "ColorBuffer log line")
one(ops, GUARD, "ColorBuffer getenv guard")

alloc_fn = ops.index("static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {")
guard_at = ops.index(GUARD)
alloc_at = ops.index(ALLOC)
assert alloc_fn < guard_at < alloc_at, "kill switch is not between allocAhb() entry and allocate"
guard_block = ops[guard_at:ops.index("return nullptr;", guard_at)]
assert "AHardwareBuffer_allocate" not in guard_block, \
    "the guarded path must return before AHardwareBuffer_allocate"
assert "return nullptr;" in ops[guard_at:alloc_at]
# The default path (env unset) still runs the unchanged allocation and its result check.
assert after(ops, ALLOC, "if (ahbRes != 0 || !ahb) {", "ColorBuffer failure check")

# ---------------------------------------------------------------------------
# 2. Host-visible AHB-first allocation: same switch, same ordering.
# ---------------------------------------------------------------------------
one(decoder, "AHB-ALLOC-KILLSWITCH-2", "AHB-first kill switch")
one(decoder, ALLOC, "AHB-first AHardwareBuffer_allocate")
one(decoder, LOG_LINE, "AHB-first log line")
one(decoder, GUARD, "AHB-first getenv guard")

first_fn = decoder.index(
    "static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {")
guard_at = decoder.index(GUARD)
alloc_at = decoder.index(ALLOC)
assert first_fn < guard_at < alloc_at, "kill switch is not between the entry and allocate"
guard_block = decoder[guard_at:decoder.index("return nullptr;", guard_at)]
assert "AHardwareBuffer_allocate" not in guard_block
assert "return nullptr;" in decoder[guard_at:alloc_at]
assert after(decoder, ALLOC, "return ahb;", "AHB-first success return")

# ---------------------------------------------------------------------------
# 3. Nullptr callers fall back safely.
# ---------------------------------------------------------------------------
# allocExternalMemory: allocAhb() failure breaks to the ordinary allocation path.
fallback = ops.index("Falling back to non-exportable allocation for ColorBuffer")
alloc_call = ops.index("AHardwareBuffer* ahb = allocAhb(")
null_check = ops.index("if (!ahb) {", alloc_call)
fallback_break = ops.index("break;", null_check)
props_at = ops.index("vkGetAndroidHardwareBufferPropertiesANDROID", null_check)
assert alloc_call < null_check < fallback < fallback_break < props_at, \
    "the !ahb fallback does not leave the AHB case before the properties query"
assert after(ops, "Falling back to non-exportable allocation for ColorBuffer",
             "bool memoryAllocated = false;", "ordinary allocation path")

# AHB-first: a null allocation keeps the memfd path and says so.
one(decoder, "AHB-FIRST-FALLBACK: no usable AHB", "AHB-first memfd fallback")
fallback_at = decoder.index("AHB-FIRST-FALLBACK: no usable AHB")
success_at = decoder.index("if (ahb && ahbBytes) {")
memfd_at = decoder.index("if (!ahbBacking) {")
assert success_at < fallback_at < memfd_at, \
    "the AHB-first success branch, fallback log and memfd path are out of order"

# Deferred image bind: on_vkCreateImage failure becomes an error, never a null bind.
deferred_at = decoder.index("VkResult performBindImageMemoryDeferredAhb(")
create_at = decoder.index("VkResult result = on_vkCreateImage(", deferred_at)
result_at = decoder.index("if (result != VK_SUCCESS) {", create_at)
assert deferred_at < create_at < result_at, "deferred AHB bind does not check on_vkCreateImage"
assert "return VK_ERROR_OUT_OF_HOST_MEMORY;" in decoder[result_at:result_at + 200], \
    "deferred AHB bind does not fail closed on image creation"
one(decoder, "if (createRes != VK_SUCCESS) return createRes;", "on_vkCreateImage failure return")

# ---------------------------------------------------------------------------
# 4. Patch script: idempotent, unique anchors, no write on rejection.
# ---------------------------------------------------------------------------
OPS_FIXTURE = """// fixture: minimal allocAhb anchor
#include <algorithm>

static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {
    return nullptr;
}
"""

DECODER_FIXTURE = """// fixture: minimal AHB-first anchor
    static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {
        if (!size || size > 0x7fffffffull) return nullptr;
        return nullptr;
    }
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_fixture(base, ops_text=OPS_FIXTURE, decoder_text=DECODER_FIXTURE):
    (base / "host" / "vulkan").mkdir(parents=True)
    (base / "host" / "vulkan" / "vk_common_operations.cpp").write_text(
        ops_text, encoding="utf-8")
    (base / "host" / "vulkan" / "vk_decoder_global_state.cpp").write_text(
        decoder_text, encoding="utf-8")


def run(fixture):
    return subprocess.run([sys.executable, str(PATCH), str(fixture)],
                          capture_output=True, text=True)


with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(fixture)

    first = run(fixture)
    assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
    patched_ops = fixture / "host" / "vulkan" / "vk_common_operations.cpp"
    patched_decoder = fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp"
    assert "AHB-ALLOC-KILLSWITCH-1" in patched_ops.read_text(encoding="utf-8")
    assert "AHB-ALLOC-KILLSWITCH-2" in patched_decoder.read_text(encoding="utf-8")
    assert "#include <cstdlib>" in patched_ops.read_text(encoding="utf-8")
    stable = (digest(patched_ops), digest(patched_decoder))

    again = run(fixture)
    assert again.returncode == 0, (again.returncode, again.stdout, again.stderr)
    assert "already patched" in again.stdout, again.stdout
    assert (digest(patched_ops), digest(patched_decoder)) == stable, "second run changed files"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(
        fixture,
        decoder_text="// fixture without the AHB-first anchor\n",
    )
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp")}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "AHB-ALLOC-KILLSWITCH-2" in rejected.stderr, rejected.stderr
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected patch modified the tree"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(
        fixture,
        ops_text=OPS_FIXTURE + """
// duplicate anchor
static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {
    return nullptr;
}
""",
    )
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp")}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "AHB-ALLOC-KILLSWITCH-1" in rejected.stderr, rejected.stderr
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected duplicate-anchor patch modified the tree"

# ---------------------------------------------------------------------------
# 5. Workflow wiring.
# ---------------------------------------------------------------------------
if args.workflow:
    workflow = args.workflow.resolve().read_text(encoding="utf-8")
else:
    candidates = [
        root / ".github/workflows/build-droidvm-mali-host.yml",
        root.parent / ".github/workflows/build-droidvm-mali-host.yml",
        root.parent / "ci/.github/workflows/build-droidvm-mali-host.yml",
    ]
    workflow_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    assert workflow_path is not None, "CI workflow not found"
    workflow = workflow_path.read_text(encoding="utf-8")

assert "scripts/*ahb_alloc_killswitch*" in workflow
assert "cp ci/scripts/*ahb_alloc_killswitch* out/" in workflow
patch_line = workflow.index("python3 -I ci/scripts/patch_ahb_alloc_killswitch.py")
test_line = workflow.index("python3 -I ci/scripts/test_ahb_alloc_killswitch.py")
first_site = workflow.index("python3 -I ci/scripts/patch_ahb_first_hostvisible.py")
assert first_site < patch_line < test_line, "kill switch must run after the AHB-first patch"
assert "test_ahb_alloc_killswitch.py source ci/.github/workflows/build-droidvm-mali-host.yml" \
    in workflow

print("PASS: GFXSTREAM_DISABLE_AHB_ALLOC short-circuits both AHardwareBuffer_allocate sites; "
      "unset path and nullptr fallbacks are intact")
