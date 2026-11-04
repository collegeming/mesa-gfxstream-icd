#!/usr/bin/env python3
"""Assert the udmabuf kernel-gate invariants on a patched gfxstream tree.

Run after patch_udmabuf_kernel_gate.py (CI runs it after the patch step and again after the
build).

Checks:
  1. the hardcoded `major != 6 || minor != 6` rule is gone and a >= 6.6 rule is in its place;
  2. the env override is exactly `GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL == "1"`, checked before
     the version test, and it still requires /dev/udmabuf to exist;
  3. HasUdmabufDevice() is defined exactly once and before its first use, so the override can
     call it;
  4. the platform guard (`#if !defined(__ANDROID__) return false`) is still ahead of everything,
     so no non-Android build changes;
  5. the udmabuf ioctl path is untouched: handleFromFd still passes memfd + offset + size, and
     the feature still requires HasUdmabufDevice() at its call site.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

creator = (root / "common/base/UdmabufCreator_linux.cpp").read_text(
    encoding="utf-8", errors="replace").replace("\r\n", "\n")
renderer = (root / "host/virtio_gpu_gfxstream_renderer.cpp").read_text(
    encoding="utf-8", errors="replace").replace("\r\n", "\n")

# 1. old rule gone, new rule present.
assert "if (major != 6 || minor != 6) {" not in creator, "the 6.6-only rule must be gone"
assert "if (major < 6 || (major == 6 && minor < 6)) {" in creator, ">= 6.6 rule missing"

# 2. the override, its exact spelling, its placement, and its device requirement.
assert 'getEnvironmentVariable("GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL") == "1"' in creator
override = creator.index("GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL")
version_test = creator.index("if (major < 6 || (major == 6 && minor < 6)) {")
assert override < version_test, "the override must be checked before the version rule"
assert "return HasUdmabufDevice();" in creator
assert creator.count('getEnvironmentVariable("GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL")') == 1,     "exactly one environment read (the rationale comment names it too)"

# 3. one definition, before first use.
assert creator.count("bool HasUdmabufDevice()") == 1, "exactly one definition"
assert creator.index("bool HasUdmabufDevice()") < creator.index("bool IsAndroidKernel6_6()"), \
    "the probe must be defined before IsAndroidKernel6_6 can call it"

# 4. non-Android builds unchanged.
android_guard = creator.index("if (ret < 0) {")
non_android = creator.index("#if !defined(__ANDROID__)")
assert non_android < creator.index("GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL"), \
    "the platform guard must stay ahead of the override"
assert "return false;" in creator[non_android:non_android + 120]

# 5. the ioctl path and the feature gate are untouched.
assert "const struct udmabuf_create create = {" in creator
assert ".offset = offset," in creator and ".size = size," in creator
assert "UDMABUF_CREATE" in creator
assert "IsAndroidKernel6_6() && gfxstream::base::HasUdmabufDevice()" in renderer, \
    "the feature must still require the device probe"
assert "VulkanAllocateHostVisibleAsUdmabuf" in renderer

report = (
    "PASS hardcoded 6.6-only rule replaced by >= 6.6\n"
    "PASS GFXSTREAM_UDMABUF_ALLOW_ANY_KERNEL=1 bypasses the version rule before it, still\n"
    "     requiring /dev/udmabuf\n"
    "PASS HasUdmabufDevice defined once, before its first use\n"
    "PASS non-Android builds still return false first\n"
    "PASS udmabuf ioctl (memfd/offset/size) and the feature's device probe unchanged\n"
)
print(report, end="")
if out:
    out.mkdir(parents=True, exist_ok=True)
    (out / "udmabuf-gate-tests.txt").write_text(report)
