#!/usr/bin/env python3
"""Tests for the render-damage patch drafts in this directory.

These tests do not build anything.  They check that each patch script present in
this directory:
  * parses/compiles,
  * applies to a fixture carrying the real anchor text,
  * is idempotent on a second run,
  * reports "needs patch" with --check before and 0 after.

A patch script that is not in this directory is skipped, so the same file works
in both CI branches: `diagnostic/render-readback-quiesce` ships only the quiesce
patch and `diagnostic/render-colorbuffer-linear` ships only the LINEAR patch.

Run:
    python scripts/test_patch_render_scripts.py
"""
import importlib.util
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

SCRIPTS_UNDER_TEST = (
    "patch_render_staging_coherent.py",
    "patch_render_colorbuffer_linear.py",
    "patch_render_readback_quiesce.py",
)


def load(name: str):
    """Import a patch script if this checkout carries it, else return None."""
    path = SCRIPTS / name
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stg = load("patch_render_staging_coherent.py")
cbl = load("patch_render_colorbuffer_linear.py")
rbq = load("patch_render_readback_quiesce.py")


def staging_fixture() -> str:
    return ("#include <vulkan/vulkan.h>\n"
            "bool VkEmulation::StagingBuffer::createLike() {\n"
            "    const VkMemoryType& memType = memProps->memoryTypes[typeIndex];\n"
            + stg.OLD +
            "    VkMemoryAllocateInfo allocInfo = {};\n"
            "}\n")


def tiling_fixture() -> str:
    return ("#include <vulkan/vulkan.h>\n"
            "bool VkEmulation::createVkColorBufferLocked() {\n"
            + cbl.OLD_TILING +
            "    return true;\n"
            "}\n")


def ahb_fixture() -> str:
    return ("#include <vulkan/vulkan.h>\n"
            "bool VkEmulation::allocExternalMemory() {\n"
            + cbl.OLD_DISJOINT +
            "    return true;\n"
            "}\n")


def readback_fixture() -> str:
    return ("#include <vulkan/vulkan.h>\n"
            "bool VkEmulation::readColorBufferToBytesLocked() {\n"
            + rbq.OLD +
            "    return true;\n"
            "}\n")


def run(script: str, root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(root), *extra],
        capture_output=True, text=True, check=False,
    )


class PatchScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "host/vulkan").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str) -> Path:
        path = self.root / "host/vulkan/vk_common_operations.cpp"
        path.write_text(text, encoding="utf-8")
        return path

    def test_scripts_compile(self) -> None:
        present = [name for name in SCRIPTS_UNDER_TEST if (SCRIPTS / name).is_file()]
        self.assertTrue(present, "no render patch scripts found next to the test")
        for name in present:
            with self.subTest(script=name):
                py_compile.compile(str(SCRIPTS / name), doraise=True)

    @unittest.skipIf(stg is None, "patch_render_staging_coherent.py not in this checkout")
    def test_staging_coherent(self) -> None:
        path = self.write(staging_fixture())
        self.assertEqual(run("patch_render_staging_coherent.py", self.root, "--check").returncode, 1)
        self.assertEqual(run("patch_render_staging_coherent.py", self.root).returncode, 0)
        text = path.read_text(encoding="utf-8")
        self.assertIn("VK_MEMORY_PROPERTY_HOST_COHERENT_BIT", text)
        self.assertNotIn("& VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);\n", text)
        # idempotent
        self.assertEqual(run("patch_render_staging_coherent.py", self.root).returncode, 0)
        self.assertEqual(run("patch_render_staging_coherent.py", self.root, "--check").returncode, 0)

    @unittest.skipIf(cbl is None, "patch_render_colorbuffer_linear.py not in this checkout")
    def test_colorbuffer_linear(self) -> None:
        path = self.write(tiling_fixture() + ahb_fixture())
        self.assertEqual(run("patch_render_colorbuffer_linear.py", self.root, "--check").returncode, 1)
        self.assertEqual(run("patch_render_colorbuffer_linear.py", self.root).returncode, 0)
        text = path.read_text(encoding="utf-8")
        self.assertIn("GFXSTREAM_COLORBUFFER_LINEAR", text)
        self.assertIn("AHB-MEMTYPE-DISJOINT", text)
        self.assertIn("GFXSTREAM_AHB_STRICT", text)
        self.assertEqual(run("patch_render_colorbuffer_linear.py", self.root, "--check").returncode, 0)

    @unittest.skipIf(rbq is None, "patch_render_readback_quiesce.py not in this checkout")
    def test_readback_quiesce(self) -> None:
        path = self.write(readback_fixture())
        self.assertEqual(run("patch_render_readback_quiesce.py", self.root, "--check").returncode, 1)
        self.assertEqual(run("patch_render_readback_quiesce.py", self.root).returncode, 0)
        text = path.read_text(encoding="utf-8")
        self.assertIn("GFXSTREAM_CIB_DUMP_QUIESCE", text)
        self.assertIn("vkDeviceWaitIdle", text)
        self.assertEqual(run("patch_render_readback_quiesce.py", self.root, "--check").returncode, 0)

    def test_missing_anchor_fails_loudly(self) -> None:
        self.write("int main() { return 0; }\n")
        present = [name for name in SCRIPTS_UNDER_TEST if (SCRIPTS / name).is_file()]
        self.assertTrue(present, "no render patch scripts found next to the test")
        for name in present:
            with self.subTest(script=name):
                self.assertEqual(run(name, self.root).returncode, 2)


if __name__ == "__main__":
    print("render patch scripts in this checkout: "
          + ", ".join(name for name in SCRIPTS_UNDER_TEST if (SCRIPTS / name).is_file()))
    unittest.main(verbosity=2)
