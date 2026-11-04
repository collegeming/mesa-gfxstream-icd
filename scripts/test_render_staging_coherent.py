#!/usr/bin/env python3
"""Tests for the P0 staging-coherence patch and the patched production source.

Checks, in this order:

1. Production invariants in host/vulkan/vk_common_operations.cpp:
   * exactly one STAGING-COHERENT-P0 marker;
   * mIsHostCoherent is derived from HOST_COHERENT_BIT, never HOST_VISIBLE_BIT;
   * every guarded readback site still calls vkInvalidateMappedMemoryRanges;
   * the guarded upload site still calls vkFlushMappedMemoryRanges.
2. The patch script on a minimal fixture: --check reports 1 before and 0 after,
   a fresh run applies exactly once, a second run is a no-op, and a file without
   the anchor is rejected with exit code 2.
3. Workflow wiring: the patch runs before its test and both the paths filter and
   the artifact copy name the script.

Usage:
    python scripts/test_render_staging_coherent.py <gfxstream-root> [workflow]
"""
import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCH = HERE / "patch_render_staging_coherent.py"

MARKER = "STAGING-COHERENT-P0"
COHERENT_ASSIGN = (
    "    mIsHostCoherent =\n"
    "        (memType.propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) != 0;"
)
VISIBLE_ASSIGN = (
    "mIsHostCoherent = (memType.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);"
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path, help="gfxstream source root")
parser.add_argument("workflow", type=Path, nargs="?", help="CI workflow file")
args = parser.parse_args()
root = args.root.resolve()

ops_path = root / "host" / "vulkan" / "vk_common_operations.cpp"
assert ops_path.is_file(), f"missing {ops_path}"
ops = ops_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# 1. Production invariants.
# ---------------------------------------------------------------------------
assert ops.count(MARKER) == 1, f"expected one {MARKER} marker, found {ops.count(MARKER)}"
assert COHERENT_ASSIGN in ops, "mIsHostCoherent is not derived from HOST_COHERENT_BIT"
assert VISIBLE_ASSIGN not in ops, "stale HOST_VISIBLE-derived mIsHostCoherent is still present"
assert "// Non-host coherent memory would require manual flush/invalidate" not in ops


staging_guards = []
pos = 0
while True:
    pos = ops.find("if (!mStaging.mIsHostCoherent) {", pos)
    if pos < 0:
        break
    staging_guards.append(ops[pos:pos + 700])
    pos += 1
assert len(staging_guards) == 3, (
    f"expected 3 mStaging guards, found {len(staging_guards)}")
invalidating = [block for block in staging_guards
                if "vkInvalidateMappedMemoryRanges" in block]
flushing = [block for block in staging_guards if "vkFlushMappedMemoryRanges" in block]
assert len(invalidating) == 2, (
    f"expected 2 readback invalidate guards, found {len(invalidating)}")
assert len(flushing) == 1, f"expected 1 upload flush guard, found {len(flushing)}"
upload_marker = "// Flush writes manually now if the memory is not coherent"
assert upload_marker in flushing[0][:300], (
    "the flush guard is no longer the ColorBuffer upload site")

# The non-global staging readback (`readBufferToBytes`) must invalidate too.
start = ops.index("if (!stagingBufferInfo.mIsHostCoherent) {")
block = ops[start:start + 700]
assert "vkInvalidateMappedMemoryRanges" in block, (
    "readBufferToBytes lost its invalidate call")

# ---------------------------------------------------------------------------
# 2. Patch script behaviour on a synthetic fixture.
# ---------------------------------------------------------------------------
def run(args_list):
    return subprocess.run(
        [sys.executable, "-I", str(PATCH), *args_list],
        capture_output=True,
        text=True,
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


with tempfile.TemporaryDirectory(prefix="staging-coherent-test-") as tmp:
    fixture_root = Path(tmp)
    fixture = fixture_root / "host" / "vulkan" / "vk_common_operations.cpp"
    fixture.parent.mkdir(parents=True)
    unpatched = (
        "// synthetic pinned-shape fixture\n"
        "void createStaging() {\n"
        "    // Non-host coherent memory would require manual flush/invalidate\n"
        "    mIsHostCoherent = (memType.propertyFlags & "
        "VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);\n"
        "}\n"
    )
    fixture.write_text(unpatched, encoding="utf-8")

    before = digest(fixture)
    check = run([str(fixture_root), "--check"])
    assert check.returncode == 1, (check.returncode, check.stdout, check.stderr)
    assert digest(fixture) == before, "--check modified the file"

    first = run([str(fixture_root)])
    assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
    patched_bytes = fixture.read_text(encoding="utf-8")
    assert MARKER in patched_bytes
    assert COHERENT_ASSIGN in patched_bytes
    assert VISIBLE_ASSIGN not in patched_bytes
    stable = digest(fixture)

    again = run([str(fixture_root)])
    assert again.returncode == 0, (again.returncode, again.stdout, again.stderr)
    assert "already patched" in again.stdout, again.stdout
    assert digest(fixture) == stable, "second run changed the file"

    after = run([str(fixture_root), "--check"])
    assert after.returncode == 0, (after.returncode, after.stdout, after.stderr)

    missing = fixture_root / "host" / "vulkan" / "vk_common_operations.cpp"
    missing.write_text("void createStaging() {}\n", encoding="utf-8")
    missing_before = digest(missing)
    rejected = run([str(fixture_root)])
    assert rejected.returncode == 2, (rejected.returncode, rejected.stdout, rejected.stderr)
    assert digest(missing) == missing_before, "failed patch modified the file"

# ---------------------------------------------------------------------------
# 3. Workflow wiring.
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

assert "scripts/*render_staging_coherent*" in workflow
assert workflow.index("python3 -I ci/scripts/patch_render_staging_coherent.py") < \
    workflow.index("python3 -I ci/scripts/test_render_staging_coherent.py")
assert "cp ci/scripts/*render_staging_coherent* out/" in workflow

print("PASS: staging coherence derives from HOST_COHERENT; both maintenance "
      "directions are guarded; patch is idempotent and wired into CI")
