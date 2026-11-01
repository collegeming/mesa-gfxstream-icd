"""CPU fake only: rewrite the hardcoded root and Android shebang in a source copy."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/native_pair_exec.sh"
ROOT = "/data/local/tmp/droidvm-native-pair-v1"
FEATURES = ("ExternalBlob:disabled,SystemBlob:disabled,"
            "VulkanAllocateHostVisibleAsUdmabuf:disabled,"
            "VulkanAllocateHostMemory:disabled,VirtioGpuNext:enabled")
GPU = ("gfxstream,context-types=gfxstream-vulkan,displays=[[mode=windowed[1280,720],"
       "refresh-rate=60,dpi=[160,160]]],vulkan=true,gles=true,pci-bar-size=4294967296,"
       "udmabuf=false,implicit-render-server=false,fixed-blob-mapping=false")


class NativePair(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="native-pair-cpu-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "bin").mkdir()
        (self.root / "lib").mkdir()
        self.binary = self.root / "bin/crosvm"
        self.binary.write_text("#!/usr/bin/python3 -I\nimport json, os, sys\n"
                               "print(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
                               "sys.exit(int(os.environ.get('FAKE_STATUS', '0')))\n")
        self.binary.chmod(0o700)
        self.lib = self.root / "lib/libgfxstream_backend.so"
        self.lib.touch()
        source = SOURCE.read_text()
        self.assertEqual(source.count(ROOT), 1)
        self.assertTrue(source.startswith("#!/system/bin/sh\n"))
        self.assertLess(len(source.splitlines()), 80)
        self.wrapper = self.root / "wrapper.sh"
        self.wrapper.write_text(source.replace("#!/system/bin/sh\n", "#!/bin/sh\n", 1)
                                .replace("root=" + ROOT + "\n", "root=" + str(self.root) + "\n", 1))
        self.wrapper.chmod(0o700)
        self.env = dict(os.environ, LD_LIBRARY_PATH="/old/app/lib:/vendor/lib64",
                        LD_PRELOAD="/app/lib/libsimpledump.so /app/lib/libcompat_a17.so",
                        ANDROID_EMU_VK_LOADER_PATH="/old/loader.so",
                        GFXSTREAM_EXTERNAL_MEMORY_MODE="wrong", GFXSTREAM_RENDERER_FEATURES="old",
                        GFXSTREAM_DEVICE_LOCAL_MEMORY_TYPE="1", GFXSTREAM_GUNYAH_PIN_RINGBLOB="0",
                        GPU_DISPLAY_COPY_MODE="vulkan", CROSVM_DISPLAY_VULKAN_LIBRARY="/old/display.so",
                        UNRELATED_HOST_VAR="retain this", GFXSTREAM_UNRELATED="retain too",
                        NATIVE_PAIR_ROOT="/must/not/override")

    def invoke(self, *args):
        return subprocess.run([str(self.wrapper), *args], env=self.env,
                              text=True, capture_output=True, timeout=10)

    def accepted(self, args):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["argv"], args)
        return record["env"]

    def rejected(self, args):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("native-pair:", result.stderr)

    def test_exact_environment_and_argv(self):
        env = self.accepted(["run", "--gpu", GPU, "--params", "a b='c';$(not-run)", "", "kernel path"])
        expected = dict(LD_LIBRARY_PATH=str(self.root / "lib"),
                        ANDROID_EMU_VK_LOADER_PATH="/system/lib64/libvulkan.so",
                        GFXSTREAM_EXTERNAL_MEMORY_MODE="AndroidAHB", GFXSTREAM_RENDERER_FEATURES=FEATURES,
                        GFXSTREAM_GUNYAH_PIN_RINGBLOB="1", GPU_DISPLAY_COPY_MODE="cpu")
        for key, value in expected.items():
            self.assertEqual(env[key], value)
        for key in ("LD_PRELOAD", "UNRELATED_HOST_VAR", "GFXSTREAM_UNRELATED", "NATIVE_PAIR_ROOT"):
            self.assertEqual(env[key], self.env[key])
        for key in ("GFXSTREAM_DEVICE_LOCAL_MEMORY_TYPE", "CROSVM_DISPLAY_VULKAN_LIBRARY"):
            self.assertNotIn(key, env)

    def test_help_and_gpu_forms(self):
        for args in (["--help"], ["run", "--help"], ["run", "--gpu=" + GPU],
                     ["run", "--gpu", "gfxstream"], ["run", "--params", "udmabuf=true"],
                     ["run", "--gpu", "gfxstream,not-udmabuf=true,other=fixed-blob-mapping=true"]):
            with self.subTest(args=args):
                self.accepted(args)

    def test_exporters_rejected(self):
        for flag in ("--vnc-server", "--android-display-service"):
            for args in (["run", flag, "value"], ["run", flag + "=value"]):
                self.rejected(args)

    def test_gpu_true_keys_rejected(self):
        for key in ("udmabuf", "implicit-render-server", "fixed-blob-mapping"):
            for value in (key + "=true", "gfxstream," + key + "=true,gles=true"):
                for args in (["run", "--gpu", value], ["run", "--gpu=" + value]):
                    self.rejected(args)

    def test_missing_and_duplicate_gpu_rejected(self):
        for args in (["run", "--gpu"], ["run", "--gpu="], ["run", "--gpu", ""],
                     ["run", "--gpu", "--help"], ["run", "--gpu", GPU, "--gpu=" + GPU]):
            self.rejected(args)

    def test_executable_and_host_library_required(self):
        self.binary.chmod(0o600)
        self.rejected(["--help"])
        self.binary.chmod(0o700)
        self.lib.chmod(0o000)
        self.rejected(["--help"])
        self.lib.unlink()
        self.rejected(["--help"])
        self.lib.mkdir()
        self.rejected(["--help"])
        self.binary.unlink()
        self.rejected(["--help"])

    def test_unset_preload_and_exit_status_preserved(self):
        self.env.pop("LD_PRELOAD")
        self.assertNotIn("LD_PRELOAD", self.accepted(["--help"]))
        self.env["FAKE_STATUS"] = "37"
        self.assertEqual(self.invoke("--help").returncode, 37)


if __name__ == "__main__":
    unittest.main(verbosity=2)
