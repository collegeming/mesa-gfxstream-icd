#!/usr/bin/env python3
"""Tests for the runtime file AHB allocation kill switch and the patched source.

Checks, in this order:

1. Production invariants:
   * exactly one injection site in the whole host/vulkan tree, in
     vk_common_operations.cpp, inside allocAhb() and before its single
     AHardwareBuffer_allocate() call;
   * the switch is the per-call existence of a regular file: the guard calls
     OffFilePath()/IsOff() unconditionally on every invocation, contains no
     static/cached decision, and returns nullptr before AHardwareBuffer_allocate;
   * the helper header stats the path on every call, treats every stat() failure
     as "not disabled", has no mutable state, and reads the environment only for
     the path override, lazily from a function-local static (no static-init
     getenv at library load);
   * vk_decoder_global_state.cpp contains no marker, no path/env lookup, no log
     line and no helper include: allocateAhbFirstMemory() and the startup path
     are byte-identical, and with the file absent allocAhb() is a pure insertion;
   * allocExternalMemory() keeps the `if (!ahb)` non-exportable fallback before
     the properties query, and the deferred image-bind path still turns
     on_vkCreateImage failure into an error.
2. The patch script on minimal fixtures:
   * a fresh run creates the helper header, inserts exactly the include and the
     guard block and nothing else: deleting those insertions reproduces the ops
     input byte for byte and leaves decoder/header as expected;
   * a second run is a no-op;
   * a missing anchor, a duplicated anchor, a divergent existing helper header,
     an old two-site decoder and an old environment gate are rejected without
     creating or modifying any file.
3. Workflow wiring: the paths filter, the helper header intent-to-add, the
   artifact copies, and the patch -> Python test -> C++ test order.

The C++ helper semantics (same process: absent -> present -> absent) are covered
by scripts/test_ahb_alloc_killswitch_file.cpp, compiled in CI.

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

HEADER_REL = "host/vulkan/ahb_alloc_off_file.h"
HELPER_ENV = "GFXSTREAM_AHB_ALLOC_OFF_FILE"
OLD_ENV = "GFXSTREAM_DISABLE_AHB_ALLOC"
DEFAULT_PATH = "/data/local/tmp/gfxstream-ahb-off"
LOG_LINE = "GFXSTREAM_AHB_ALLOC_DISABLED file=%s"
BEGIN = "    // AHB-ALLOC-KILLSWITCH-1-BEGIN\n"
END = "    // AHB-ALLOC-KILLSWITCH-1-END\n"
MARKER = "AHB-ALLOC-KILLSWITCH"
INCLUDE = '#include "ahb_alloc_off_file.h"\n'
INCLUDE_ANCHOR = (
    "#ifdef __ANDROID__\n"
    "// Allocate an AHardwareBuffer matching the given image's format, extent and usage.\n"
)
INCLUDE_BLOCK = (
    "#ifdef __ANDROID__\n"
    + INCLUDE
    + "// Allocate an AHardwareBuffer matching the given image's format, extent and usage.\n"
)
PATH_CALL = "gfxstream::ahb_alloc_killswitch::OffFilePath()"
DECISION_CALL = "gfxstream::ahb_alloc_killswitch::IsOff(ahbOffFile)"
ALLOC = "AHardwareBuffer_allocate(&desc, &ahb)"
ALLOC_FN = "static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {"
FIRST_FN = "static AHardwareBuffer* allocateAhbFirstMemory(VkDeviceSize size) {"
OFF_FN = "inline const char* OffFilePath() {"
IS_OFF_FN = "inline bool IsOff(const char* path) {"

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="gfxstream source root")
parser.add_argument("workflow", type=Path, nargs="?", help="CI workflow file")
args = parser.parse_args()
root = args.root.resolve()

ops_path = root / "host/vulkan/vk_common_operations.cpp"
decoder_path = root / "host/vulkan/vk_decoder_global_state.cpp"
header_path = root / HEADER_REL
ops = ops_path.read_text(encoding="utf-8")
decoder = decoder_path.read_text(encoding="utf-8")
header = header_path.read_text(encoding="utf-8")


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
# 1. Exactly one injection site, in allocAhb() only, checked on every call.
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
one(ops, INCLUDE_BLOCK, "helper include inside the Android block")
one(ops, PATH_CALL, "per-call path lookup")
one(ops, DECISION_CALL, "per-call file decision")
one(ops, ALLOC, "AHardwareBuffer_allocate")
one(ops, "if (ahbRes != 0 || !ahb) {", "ColorBuffer failure check")

sig_at, open_at, close_at = function_body(ops, ALLOC_FN)
begin_at = ops.index(BEGIN)
end_at = ops.index(END) + len(END)
path_at = ops.index(PATH_CALL)
alloc_at = ops.index(ALLOC)
assert open_at < begin_at < end_at < close_at, "kill switch is outside allocAhb()"
assert open_at < path_at < close_at, "the file check is not inside allocAhb()"
assert end_at < alloc_at, "the guarded block must precede AHardwareBuffer_allocate"
assert ops.index(INCLUDE) < sig_at, "the helper include must precede allocAhb()"

guard_block = ops[begin_at:end_at]
assert "static" not in guard_block, (
    "the decision must not be cached in a static; the file is checked on every call")
assert "return nullptr;" in guard_block, "the guarded block does not fail closed"
assert "AHardwareBuffer_allocate(" not in guard_block, (
    "the guarded path must return before AHardwareBuffer_allocate")
assert OLD_ENV not in ops, "the old environment switch must be gone from allocAhb()"
assert "getenv(" not in ops, (
    "allocAhb() must not read the environment itself; only the header resolves the path")

# Unset: fall through to the unchanged allocation and its failure handling.
assert ops.count(ALLOC) == 1, "unexpected extra AHardwareBuffer_allocate site"
assert after(ops, ALLOC, "if (ahbRes != 0 || !ahb) {", "ColorBuffer failure check")

# ---------------------------------------------------------------------------
# 2. Helper header: stat() every call, failure is "not disabled", lazy path only.
# ---------------------------------------------------------------------------
assert header_path.is_file(), "helper header was not created"
one(header, f'getenv("{HELPER_ENV}")', "path override lookup")
one(header, f'"{DEFAULT_PATH}"', "default off file path")
one(header, "struct stat st = {};", "stat buffer")
one(header, "stat(path, &st) == 0 && S_ISREG(st.st_mode)", "regular-file decision")
assert header.count("getenv(") == 1, "exactly one environment lookup (path override only)"
assert "static bool" not in header and "static const bool" not in header, (
    "the file decision must not be cached")
assert "access(" not in header, "stat() with S_ISREG is the chosen probe"

sig_at, open_at, close_at = function_body(header, OFF_FN)
getenv_at = header.index("getenv(")
assert open_at < getenv_at < close_at, (
    "the path override is not resolved lazily inside OffFilePath()")
assert "static const char* const kPath" in header[open_at:close_at], (
    "the path cache is not a function-local static")
assert header.index("static const char* const kPath") > open_at, (
    "no namespace-scope static initializer may call getenv")

sig_at, open_at, close_at = function_body(header, IS_OFF_FN)
stat_at = header.index("stat(path, &st)")
assert open_at < stat_at < close_at, "stat() must run inside IsOff() on every call"
assert "GFXSTREAM_WARNING" not in header and "GFXSTREAM_AHB_ALLOC_DISABLED" not in header, (
    "the helper must not log when the switch is absent or the probe fails")
assert "printf" not in header, "the helper must not log"

# ---------------------------------------------------------------------------
# 3. The AHB-first / startup path is not switched and keeps its own fallback.
# ---------------------------------------------------------------------------
for needle, label in (
    (MARKER, "kill switch marker"),
    (OLD_ENV, "old environment gate"),
    (HELPER_ENV, "path override lookup"),
    ("GFXSTREAM_AHB_ALLOC_DISABLED", "kill switch log line"),
    ("ahb_alloc_off_file.h", "helper include"),
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
# 4. The nullptr caller falls back safely.
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
# 5. Patch script: unique anchors, pure insertion, idempotent, no write on rejection.
# ---------------------------------------------------------------------------
OPS_FIXTURE = """// fixture: minimal allocAhb anchor
#ifdef __ANDROID__
// Allocate an AHardwareBuffer matching the given image's format, extent and usage.
static AHardwareBuffer* allocAhb(const VkImageCreateInfo* imageCreateInfo) {
    return nullptr;
}
#endif
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
    patched_header = fixture / HEADER_REL
    patched = patched_ops.read_text(encoding="utf-8")
    assert patched_header.is_file(), "the patch did not create the helper header"
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
        "the file-absent path would not be byte-identical to the baseline")

    stable = (digest(patched_ops), digest(patched_decoder), digest(patched_header))
    again = run(fixture)
    assert again.returncode == 0, (again.returncode, again.stdout, again.stderr)
    assert "already patched" in again.stdout, again.stdout
    assert (digest(patched_ops), digest(patched_decoder), digest(patched_header)) == stable, \
        "second run changed files"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(fixture, ops_text='#ifdef __ANDROID__\n// no allocAhb anchor\n#endif\n')
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp")}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "allocAhb" in rejected.stderr, rejected.stderr
    assert not (fixture / HEADER_REL).exists(), "a rejected patch created the helper header"
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
    assert not (fixture / HEADER_REL).exists(), "a rejected patch created the helper header"
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected duplicate-anchor patch modified the tree"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(fixture)
    divergent = fixture / HEADER_REL
    divergent.write_text("// wrong helper\n", encoding="utf-8")
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp",
        divergent)}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "does not match" in rejected.stderr, rejected.stderr
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected divergent-header patch modified the tree"

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
    assert not (fixture / HEADER_REL).exists(), "a rejected patch created the helper header"
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected old-two-site patch modified the tree"

with tempfile.TemporaryDirectory() as tmp:
    fixture = Path(tmp)
    make_fixture(
        fixture,
        ops_text=OPS_FIXTURE.replace(
            "// fixture: minimal allocAhb anchor\n",
            "// fixture: minimal allocAhb anchor\n"
            "static const bool oldGate = getenv(\"GFXSTREAM_DISABLE_AHB_ALLOC\") != nullptr;\n"),
    )
    before = {p: digest(p) for p in (
        fixture / "host" / "vulkan" / "vk_common_operations.cpp",
        fixture / "host" / "vulkan" / "vk_decoder_global_state.cpp")}
    rejected = run(fixture)
    assert rejected.returncode != 0, rejected.stdout
    assert "old GFXSTREAM_DISABLE_AHB_ALLOC environment gate" in rejected.stderr, rejected.stderr
    assert not (fixture / HEADER_REL).exists(), "a rejected patch created the helper header"
    after = {p: digest(p) for p in before}
    assert after == before, "a rejected old-environment-gate patch modified the tree"

# ---------------------------------------------------------------------------
# 6. Workflow wiring.
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
assert "cp source/host/vulkan/ahb_alloc_off_file.h out/" in workflow
assert "test_ahb_alloc_killswitch_file.cpp" in workflow
assert "out/ahb-alloc-killswitch-file-tests.txt" in workflow
patch_line = workflow.index("python3 -I ci/scripts/patch_ahb_alloc_killswitch.py")
test_line = workflow.index("python3 -I ci/scripts/test_ahb_alloc_killswitch.py")
cxx_line = workflow.index("ci/scripts/test_ahb_alloc_killswitch_file.cpp")
first_site = workflow.index("python3 -I ci/scripts/patch_ahb_first_hostvisible.py")
assert first_site < patch_line < test_line < cxx_line, (
    "kill switch must run after the AHB-first patch and before its C++ helper test")
assert "git -C source add -N host/vulkan/ahb_alloc_off_file.h" in workflow, (
    "the new helper header must be intent-to-add so the provenance patch includes it")
assert "test_ahb_alloc_killswitch.py source ci/.github/workflows/build-droidvm-mali-host.yml" \
    in workflow

print("PASS: the AHB allocation switch is a per-call stat() of a runtime file; "
      "no environment decision, no static-init getenv, no cached result, the startup "
      "AHB-first path is untouched, the file-absent path is a pure insertion that still "
      "reaches AHardwareBuffer_allocate, and the C++ helper test covers appearance and "
      "removal in one process")
