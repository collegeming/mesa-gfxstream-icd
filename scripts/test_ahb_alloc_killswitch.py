#!/usr/bin/env python3
"""Tests for the lazy AHB allocation kill switch and the patched source.

Checks, in this order:

1. Production invariants:
   * exactly one injection site in the whole host/vulkan tree, in
     vk_common_operations.cpp, inside allocAhb() and before its single
     AHardwareBuffer_allocate() call;
   * the switch is syntactic sugar for a function-local lazy static:
       static const bool disableAhbAlloc = getenv("GFXSTREAM_DISABLE_AHB_ALLOC") != nullptr;
     so getenv() is not called from any static initializer and the environment is
     read at most once, on the first explicit allocation;
   * vk_decoder_global_state.cpp contains no marker, no GFXSTREAM_DISABLE_AHB_ALLOC
     lookup and no GFXSTREAM_AHB_ALLOC_DISABLED log: allocateAhbFirstMemory(), the
     startup/first-allocation path, is not switched;
   * the guarded block returns nullptr and never reaches AHardwareBuffer_allocate;
     unset falls through to the unchanged allocation and its failure check;
   * allocExternalMemory() keeps the `if (!ahb)` non-exportable fallback before the
     properties query, the AHB-first branch keeps its own AHB-FIRST-FALLBACK, and
     the deferred image-bind path still turns on_vkCreateImage failure into an error.
2. The patch script on minimal fixtures:
   * a fresh run inserts exactly the guard block and the <cstdlib> include and
     nothing else: deleting those two insertions reproduces the input byte for
     byte, which is what makes the unset path control-flow equivalent;
   * a second run is a no-op;
   * a missing anchor, a duplicated anchor, and an old two-site tree are rejected
     without modifying any file.
3. Workflow wiring: the paths filter, the artifact copy, and the patch/test order.

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

ENV_NAME = "GFXSTREAM_DISABLE_AHB_ALLOC"
LOG_LINE = "GFXSTREAM_AHB_ALLOC_DISABLED: shape=%ux%u format=%u usage=0x%llx"
BEGIN = "    // AHB-ALLOC-KILLSWITCH-1-BEGIN\n"
END = "    // AHB-ALLOC-KILLSWITCH-1-END\n"
MARKER = "AHB-ALLOC-KILLSWITCH"
INCLUDE = "#include <cstdlib>\n"
GUARD_DECL = (
    "    static const bool disableAhbAlloc = "
    'getenv("GFXSTREAM_DISABLE_AHB_ALLOC") != nullptr;\n'
)
GUARD_IF = "if (disableAhbAlloc) {"
ALLOC = "AHardwareBuffer_allocate(&desc, &ahb)"
ALLOC_FN = "static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {"
FIRST_FN = "static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {"

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="gfxstream source root")
parser.add_argument("workflow", type=Path, nargs="?", help="CI workflow file")
args = parser.parse_args()
root = args.root.resolve()

ops_path = root / "host/vulkan/vk_common_operations.cpp"
decoder_path = root / "host/vulkan/vk_decoder_global_state.cpp"
ops = ops_path.read_text(encoding="utf-8")
decoder = decoder_path.read_text(encoding="utf-8")


def one(text, needle, label):
    count = text.count(needle)
    assert count == 1, f"{label}: expected 1 occurrence of {needle!r}, found {count}"


def after(text, first, second, label):
    a = text.index(first)
    b = text.index(second)
    assert a < b, f"{label}: {first!r} must precede {second!r}"
    return True


def function_body(text, signature):
    """Return (signature_at, open_brace_at, close_brace_at) for a top-level function."""
    sig = text.index(signature)
    open_at = text.index("{", sig)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return sig, open_at, i
    raise AssertionError(f"unbalanced braces for {signature!r}")


# ---------------------------------------------------------------------------
# 1. Exactly one injection site, in allocAhb() only.
# ---------------------------------------------------------------------------
hits = []
for path in sorted((root / "host" / "vulkan").rglob("*")):
    if path.suffix not in (".cpp", ".h", ".cc", ".cxx") or not path.is_file():
        continue
    try:
        count = path.read_text(encoding="utf-8", errors="ignore").count(MARKER)
    except OSError:
        continue
    if count:
        hits.append((path.resolve(), count))
assert hits == [(ops_path.resolve(), 2)], (
    f"expected the kill switch only in {ops_path} (BEGIN+END), found {hits}")

one(ops, BEGIN, "kill switch BEGIN marker")
one(ops, END, "kill switch END marker")
one(ops, LOG_LINE, "kill switch log line")
one(ops, GUARD_DECL, "lazy function-local static guard")
one(ops, f'getenv("{ENV_NAME}")', "single env lookup")
one(ops, ALLOC, "AHardwareBuffer_allocate")
one(ops, "if (ahbRes != 0 || !ahb) {", "ColorBuffer failure check")

sig_at, open_at, close_at = function_body(ops, ALLOC_FN)
begin_at = ops.index(BEGIN)
end_at = ops.index(END) + len(END)
env_at = ops.index(f'getenv("{ENV_NAME}")')
alloc_at = ops.index(ALLOC)
assert open_at < begin_at < end_at < close_at, "kill switch is outside allocAhb()"
assert open_at < env_at < close_at, (
    "getenv() is not inside allocAhb(): it must not run from a static initializer")
assert end_at < alloc_at, "the guarded block must precede AHardwareBuffer_allocate"

guard_block = ops[begin_at:end_at]
assert GUARD_IF in guard_block, "the guarded block does not branch on the lazy static"
assert "return nullptr;" in guard_block, "the guarded block does not fail closed"
assert "AHardwareBuffer_allocate(" not in guard_block, (
    "the guarded path must return before AHardwareBuffer_allocate")

# Unset: fall through to the unchanged allocation and its failure handling.
assert ops.count(ALLOC) == 1, "unexpected extra AHardwareBuffer_allocate site"
assert after(ops, ALLOC, "if (ahbRes != 0 || !ahb) {", "ColorBuffer failure check")

# ---------------------------------------------------------------------------
# 2. The AHB-first / startup path is not switched and keeps its own fallback.
# ---------------------------------------------------------------------------
for needle, label in (
    (MARKER, "kill switch marker"),
    (ENV_NAME, "kill switch env lookup"),
    ("GFXSTREAM_AHB_ALLOC_DISABLED", "kill switch log line"),
):
    assert needle not in decoder, (
        f"{label} leaked into vk_decoder_global_state.cpp; "
        "allocateAhbFirstMemory() must stay byte-identical")
assert FIRST_FN in decoder, "AHB-first allocation function is missing"
one(decoder, "AHB-FIRST-FALLBACK: no usable AHB", "AHB-first memfd fallback")

success_at = decoder.index("if (ahb && ahbBytes) {")
fallback_at = decoder.index("AHB-FIRST-FALLBACK: no usable AHB")
memfd_at = decoder.index("if (!ahbBacking) {")
assert success_at < fallback_at < memfd_at, (
    "the AHB-first success branch, fallback log and memfd path are out of order")

# ---------------------------------------------------------------------------
# 3. The nullptr caller falls back safely.
# ---------------------------------------------------------------------------
fallback = ops.index("Falling back to non-exportable allocation for ColorBuffer")
alloc_call = ops.index("AHardwareBuffer* ahb = allocAhb(")
null_check = ops.index("if (!ahb) {", alloc_call)
fallback_break = ops.index("break;", null_check)
props_at = ops.index("vkGetAndroidHardwareBufferPropertiesANDROID", null_check)
assert alloc_call < null_check < fallback < fallback_break < props_at, (
    "the !ahb fallback does not leave the AHB case before the properties query")
assert after(ops, "Falling back to non-exportable allocation for ColorBuffer",
             "bool memoryAllocated = false;", "ordinary allocation path")

# Deferred image bind: on_vkCreateImage failure becomes an error, never a null bind.
deferred_at = decoder.index("VkResult performBindImageMemoryDeferredAhb(")
create_at = decoder.index("VkResult result = on_vkCreateImage(", deferred_at)
result_at = decoder.index("if (result != VK_SUCCESS) {", create_at)
assert deferred_at < create_at < result_at, "deferred AHB bind does not check on_vkCreateImage"
assert "return VK_ERROR_OUT_OF_HOST_MEMORY;" in decoder[result_at:result_at + 200], (
    "deferred AHB bind does not fail closed on image creation")
one(decoder, "if (createRes != VK_SUCCESS) return createRes;", "on_vkCreateImage failure return")

# ---------------------------------------------------------------------------
# 4. Patch script: unique anchors, pure insertion, idempotent, no write on rejection.
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
    patched = patched_ops.read_text(encoding="utf-8")
    assert MARKER in patched and INCLUDE in patched
    assert patched_decoder.read_text(encoding="utf-8") == DECODER_FIXTURE, (
        "the patch modified the AHB-first/startup source")

    # Unset-path equivalence: the patch is a pure insertion of the include and the
    # guard block. Removing exactly those two insertions must reproduce the input.
    b = patched.index(BEGIN)
    e = patched.index(END) + len(END)
    stripped = patched[:b] + patched[e:]
    stripped = stripped.replace(INCLUDE, "", 1)
    assert stripped == OPS_FIXTURE, (
        "the patch changed more than the include and the guard block; "
        "the env-unset path would not be byte-identical to the baseline")

    stable = (digest(patched_ops), digest(patched_decoder))
    again = run(fixture)
    assert again.returncode == 0, (again.returncode, again.stdout, again.stderr)
    assert "already patched" in again.stdout, again.stdout
    assert (digest(patched_ops), digest(patched_decoder)) == stable, "second run changed files"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(fixture, ops_text="#include <algorithm>\n// no allocAhb anchor\n")
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp")}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "allocAhb" in rejected.stderr, rejected.stderr
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
    assert "allocAhb" in rejected.stderr, rejected.stderr
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected duplicate-anchor patch modified the tree"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(
        fixture,
        decoder_text=DECODER_FIXTURE + """
// old two-site tree: the startup path still carries the kill switch
static void oldSite() { if (getenv("GFXSTREAM_DISABLE_AHB_ALLOC")) {} }  // AHB-ALLOC-KILLSWITCH-2
""",
    )
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp")}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "AHB-ALLOC-KILLSWITCH-2" in rejected.stderr, rejected.stderr
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected old-two-site patch modified the tree"

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

print("PASS: one lazy GFXSTREAM_DISABLE_AHB_ALLOC check in allocAhb() only; "
      "no static-init getenv, the startup AHB-first path is untouched, and the unset "
      "path is a pure insertion that still reaches AHardwareBuffer_allocate")
