"""Unit coverage for the manifest toolchain's relocatable WebGPU SDK payload."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from webgpu_sdk import stage_webgpu_sdk


class WebGpuSdkTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self._write_sources()

    def write(self, relative, data="fixture"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def _write_sources(self):
        for relative in (
                "third_party/wgpu_native/upstream/ffi/wgpu.h",
                "third_party/wgpu_native/upstream/ffi/webgpu-headers/webgpu.h",
                "third_party/wgpu_native/runtime/vulkan_loader.c",
                "third_party/wgpu_native/runtime/vulkan_loader.h",
                "third_party/wgpu_native/runtime/win64_bridge.c",
                "third_party/wgpu_native/runtime/win64_bridge.h",
                "third_party/lavapipe/register.c",
                "third_party/lavapipe/register.h",
                "o/webgpu/generated/vulkan_signatures.inc",
                "third_party/wgpu_native/upstream/LICENSE.APACHE",
                "third_party/wgpu_native/upstream/LICENSE.MIT",
                "third_party/wgpu_native/upstream/ffi/webgpu-headers/LICENSE",
                "o/webgpu/NOTICE.txt",
                "o/webgpu/licenses/dependency/LICENSE",
                "o/lavapipe/licenses/mesa/LICENSE",
                "o/lavapipe/libcosmo_lavapipe_registration.a",
                "o/lavapipe/build/libvulkan_lvp.a",
                "o/llvm-cosmo/build/lib/libLLVMCore.a",
                "o/rust-ape/build/libwgpu_native.a",
                "o/rust-ape/sdk/generated/shim-runtime-x86_64.o"):
            self.write(relative, relative + "\n")
        self.link_manifest = self.write("o/lavapipe/LINK.json", json.dumps({
            "static": True,
            "external_vulkan_loader": False,
            "provider": "embedded",
            "mesa_version": "25.2.8",
            "llvm_version": "19.1.7",
            "licenses": "/stale/checkout/o/lavapipe/licenses",
            "link_args": [
                "/stale/checkout/o/lavapipe/libcosmo_lavapipe_registration.a",
                "-Wl,--whole-archive",
                "/stale/checkout/o/lavapipe/build/libvulkan_lvp.a",
                "-Wl,--no-whole-archive",
                "-Wl,--start-group",
                "/stale/checkout/o/llvm-cosmo/build/lib/libLLVMCore.a",
                "-Wl,--end-group",
                "-lm",
            ],
        }))
        self.wgpu_archive = self.root / "o/rust-ape/build/libwgpu_native.a"
        self.rust_sdk = self.root / "o/rust-ape/sdk"
        self.output = self.root / "staged/webgpu"

    def stage(self):
        return stage_webgpu_sdk(self.link_manifest, self.wgpu_archive,
                                self.rust_sdk, self.output, self.root)

    def test_stages_relocatable_archives_runtime_sources_and_notices(self):
        metadata = self.stage()

        arguments = (self.output / "link.args").read_text().splitlines()
        self.assertEqual(arguments[0], "lib/wgpu/libwgpu_native.a")
        self.assertIn("lib/mesa/libcosmo_lavapipe_registration.a", arguments)
        self.assertIn("lib/mesa/libvulkan_lvp.a", arguments)
        self.assertIn("lib/llvm/libLLVMCore.a", arguments)
        self.assertIn("lib/rust-ape/shim-runtime-x86_64.o", arguments)
        self.assertFalse(any("stale/checkout" in argument for argument in arguments))
        self.assertTrue((self.output / "runtime/lavapipe_register.c").is_file())
        self.assertTrue((self.output / "include/cosmo_lavapipe.h").is_file())
        self.assertTrue((self.output / "licenses/wgpu-dependencies/dependency/LICENSE").is_file())
        self.assertTrue((self.output / "licenses/software-vulkan/mesa/LICENSE").is_file())
        self.assertEqual(metadata["capabilities"], [
            "compute", "offscreen-rgba8-raster", "texture-readback"])

        for relative, expected in metadata["files"].items():
            actual = hashlib.sha256((self.output / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

    def test_rejects_shared_library_link_inputs(self):
        manifest = json.loads(self.link_manifest.read_text())
        manifest["link_args"].append("-Wl,/tmp/libvulkan.so")
        self.link_manifest.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(ValueError, "shared WebGPU link input"):
            self.stage()

    def test_requires_lavapipe_registration_sources(self):
        (self.root / "third_party/lavapipe/register.h").unlink()

        with self.assertRaisesRegex(ValueError, "register.h"):
            self.stage()


if __name__ == "__main__":
    unittest.main()
