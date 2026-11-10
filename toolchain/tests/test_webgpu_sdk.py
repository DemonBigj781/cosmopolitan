"""Unit coverage for the manifest toolchain's relocatable WebGPU SDK payload."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from webgpu_sdk import stage_webgpu_sdk
import webgpu_sdk
import source_receipt


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


    def make_prepared_build(self, patched=True):
        foundation = self.root / "third_party/wgpu_native"
        for name in source_receipt.FOUNDATION_INPUTS:
            self.write("third_party/wgpu_native/" + name,
                       '{"sources": []}' if name == "IMPORT.json" else "fixture " + name)
        out = self.root / "o/webgpu"
        shutil.copytree(foundation / "upstream", out / "source")
        self.write("o/webgpu/source/ffi/wgpu.h", "prepared wgpu declarations\n")
        self.write("o/webgpu/source/ffi/webgpu-headers/webgpu.h", "prepared WebGPU declarations\n")
        self.write("o/webgpu/source/LICENSE.MIT", "prepared native author notice\n")
        self.write("o/webgpu/source/ffi/webgpu-headers/LICENSE", "prepared header author notice\n")
        self.write("o/webgpu/source/NOTICE.txt", "application source notice\n")
        self.write("o/webgpu/source/ffi/webgpu-headers/NOTICE.extra", "application header notice\n")
        self.write("o/webgpu/source/Cargo.lock", "fixture prepared lock")
        self.write("o/webgpu/cargo.cosmo.toml", "fixture generated config")
        crates = {}
        for name, (version, _) in source_receipt.PATCHED_CRATES.items():
            root = out / "deps" / (name + "-" + version)
            self.write(root.relative_to(self.root) / "src/lib.rs", "fixture crate source")
            crates[name] = root
        manifest = None
        if patched:
            patch = self.write("application/change.patch", "fixture patch provenance, not applied by this test")
            manifest = self.write("application/patches.json", json.dumps({"schema_version": 1, "patches": [{
                "name": "application/change.patch", "path": patch.name,
                "sha256": source_receipt.file_sha256(patch)}]}))
        with source_receipt.frozen_application_patches(manifest) as (patches, provenance):
            shutil.copytree(patches, out / "application-patches")
            shutil.copytree(patches, out / "licenses/application-patches")
            prepared = source_receipt.write_prepared(out, foundation, crates, provenance)
        build = {
            "runtime": {"kind": "stock-sdk"},
            "wgpu_archive_sha256": webgpu_sdk.sha256(self.wgpu_archive),
            "prepared_receipt_sha256": source_receipt.file_sha256(out / "PREPARED.json"),
            "application_patch_licenses_path": "licenses/application-patches",
            **{key: prepared[key] for key in ("application_patches", "application_patch_order",
                "prepared_source_sha256", "prepared_lock_sha256", "foundation_inputs")},
        }
        self.write("o/webgpu/BUILD.json", json.dumps(build))
        return build

    def source_profile_mocks(self, build):
        # Runtime production/binding has separate tests. These mocks isolate
        # the SDK staging policy without compiling an SDK or native archives.
        sys.path.insert(0, str(ROOT / "third_party/rust_ape"))
        import runtime_profile
        runtime = {"kind": "source-built", "runtime_identity": "fixture-runtime", "runtime_abi": "fixture-abi"}
        link = json.loads(self.link_manifest.read_text())
        link["runtime"] = runtime
        self.link_manifest.write_text(json.dumps(link))
        shim = self.rust_sdk / "generated/shim-runtime-x86_64.o"
        build.update({"runtime": runtime, "embedded_software_vulkan": True, "software_vulkan": link,
                      "rust_shim_sha256": {shim.name: webgpu_sdk.sha256(shim)}})
        self.write("o/webgpu/BUILD.json", json.dumps(build))
        stack = ExitStack()
        stack.enter_context(mock.patch.object(webgpu_sdk, "selected_runtime", return_value=runtime))
        stack.enter_context(mock.patch.object(webgpu_sdk, "archive_sha256", side_effect=webgpu_sdk.sha256))
        stack.enter_context(mock.patch.object(webgpu_sdk, "dependency_hashes", return_value={}))
        stack.enter_context(mock.patch.object(runtime_profile, "verify_binding", return_value=runtime))
        stack.enter_context(mock.patch.object(runtime_profile, "expected_shim_objects", return_value=[shim]))
        return stack

    def test_prepared_headers_notices_and_exact_receipts_are_staged(self):
        build = self.make_prepared_build()
        metadata = self.stage()
        self.assertEqual((self.output / "include/wgpu.h").read_text(), "prepared wgpu declarations\n")
        self.assertEqual((self.output / "include/webgpu.h").read_text(), "prepared WebGPU declarations\n")
        self.assertEqual((self.output / "licenses/wgpu-native/LICENSE.MIT").read_text(),
                         "prepared native author notice\n")
        self.assertEqual((self.output / "licenses/webgpu-headers/LICENSE").read_text(),
                         "prepared header author notice\n")
        self.assertEqual((self.output / "licenses/wgpu-native/NOTICE.txt").read_text(),
                         "application source notice\n")
        self.assertEqual((self.output / "licenses/wgpu-native/BUILD_NOTICE.txt").read_bytes(),
                         (self.root / "o/webgpu/NOTICE.txt").read_bytes())
        self.assertTrue((self.output / "licenses/webgpu-headers/NOTICE.extra").is_file())
        for name in ("PREPARED.json", "BUILD.json"):
            self.assertEqual((self.output / "provenance/webgpu" / name).read_bytes(),
                             (self.root / "o/webgpu" / name).read_bytes())
        self.assertEqual(metadata["header_source"], "prepared")
        self.assertEqual(metadata["application_patches"], build["application_patches"])
        self.assertEqual(source_receipt.tree_receipt(self.output / metadata["application_patch_licenses_path"]),
                         source_receipt.tree_receipt(self.root / "o/webgpu/application-patches"))
        for relative, expected in metadata["files"].items():
            self.assertEqual(webgpu_sdk.sha256(self.output / relative), expected)

    def test_empty_application_map_still_uses_prepared_headers_when_receipted(self):
        self.make_prepared_build(patched=False)
        metadata = self.stage()
        self.assertEqual(metadata["application_patches"], {})
        self.assertEqual((self.output / "include/wgpu.h").read_text(), "prepared wgpu declarations\n")

    def test_source_runtime_stages_prepared_headers(self):
        build = self.make_prepared_build()
        with self.source_profile_mocks(build):
            metadata = self.stage()
        self.assertEqual(metadata["header_source"], "prepared")
        self.assertEqual(metadata["runtime"]["kind"], "source-built")

    def test_source_runtime_cannot_fall_back_to_pristine_without_receipt(self):
        build = {"wgpu_archive_sha256": webgpu_sdk.sha256(self.wgpu_archive)}
        with self.source_profile_mocks(build):
            with self.assertRaisesRegex(ValueError, "requires a prepared receipt hash"):
                self.stage()
        self.assertFalse(self.output.exists())

    def test_application_patched_build_cannot_omit_prepared_receipt(self):
        self.write("o/webgpu/BUILD.json", json.dumps({"application_patches": {"patch": "a" * 64}}))
        with self.assertRaisesRegex(ValueError, "requires a prepared receipt hash"):
            self.stage()

    def test_changed_prepared_header_before_staging_preserves_old_output(self):
        self.make_prepared_build()
        self.write("staged/webgpu/previous", "previous SDK stays intact")
        self.write("o/webgpu/source/ffi/wgpu.h", "unreceipted header")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage()
        self.assertEqual((self.output / "previous").read_text(), "previous SDK stays intact")

    def test_changed_header_during_staging_is_detected_after_copy(self):
        self.make_prepared_build()
        original_copy = webgpu_sdk.copy_file
        header = self.root / "o/webgpu/source/ffi/wgpu.h"
        def copy_and_change(source, destination, expected=None):
            result = original_copy(source, destination, expected)
            if source == header:
                header.write_text("changed after verified header was copied")
            return result
        with mock.patch.object(webgpu_sdk, "copy_file", side_effect=copy_and_change):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.stage()
        self.assertFalse(self.output.exists())

    def test_copy_checks_header_digest_even_if_source_is_restored(self):
        self.make_prepared_build()
        original_copy = webgpu_sdk.copy_file
        header = self.root / "o/webgpu/source/ffi/wgpu.h"
        def change_copy_restore(source, destination, expected=None):
            if source != header:
                return original_copy(source, destination, expected)
            original = header.read_bytes()
            try:
                header.write_text("substituted only while copying")
                return original_copy(source, destination, expected)
            finally:
                header.write_bytes(original)
        with mock.patch.object(webgpu_sdk, "copy_file", side_effect=change_copy_restore):
            with self.assertRaisesRegex(ValueError, "changed while copying"):
                self.stage()
        self.assertFalse(self.output.exists())

    def test_prepared_header_symlink_is_rejected(self):
        self.make_prepared_build()
        header = self.root / "o/webgpu/source/ffi/wgpu.h"
        header.unlink()
        header.symlink_to(self.root / "third_party/wgpu_native/upstream/ffi/wgpu.h")
        with self.assertRaisesRegex(ValueError, "Non-regular"):
            self.stage()

    def test_mismatched_build_patch_map_is_rejected(self):
        build = self.make_prepared_build()
        build["application_patches"] = {}
        self.write("o/webgpu/BUILD.json", json.dumps(build))
        with self.assertRaisesRegex(ValueError, "differs from the prepared source receipt"):
            self.stage()

    def test_missing_packaged_patch_provenance_is_rejected(self):
        self.make_prepared_build()
        shutil.rmtree(self.root / "o/webgpu/licenses/application-patches")
        with self.assertRaisesRegex(ValueError, "ordinary source directory"):
            self.stage()

    def test_custom_build_manifest_name_preserves_exact_build_bytes(self):
        self.make_prepared_build()
        custom = self.root / "o/webgpu/custom-build.json"
        (self.root / "o/webgpu/BUILD.json").rename(custom)
        stage_webgpu_sdk(self.link_manifest, self.wgpu_archive, self.rust_sdk, self.output, self.root,
                         build_manifest=custom)
        self.assertEqual((self.output / "provenance/webgpu/BUILD.json").read_bytes(), custom.read_bytes())


if __name__ == "__main__":
    unittest.main()
