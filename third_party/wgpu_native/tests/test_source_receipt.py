#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Local fixture coverage; no downloads, Cargo, compilation or source preparation."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare
import source_receipt as receipt


class PatchInputs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = self.root / "change.patch"
        self.patch.write_bytes(b"--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n")
        self.entry = {"name": "app/patches/change.patch", "path": "change.patch",
                      "sha256": receipt.file_sha256(self.patch)}
        self.manifest = self.root / "input.json"
        self.write([self.entry])

    def write(self, entries):
        self.manifest.write_text(json.dumps({"schema_version": 1, "patches": entries}))

    def test_default_is_empty(self):
        with receipt.frozen_application_patches() as (root, provenance):
            self.assertEqual(provenance["patches"], [])
            self.assertIsNone(provenance["manifest_sha256"])
            self.assertFalse((root / "INPUT_MANIFEST.json").exists())

    def test_order_and_original_bytes_are_owned(self):
        second = {**self.entry, "name": "app/patches/second.patch"}
        self.write([second, self.entry])
        original = self.patch.read_bytes()
        manifest = self.manifest.read_bytes()
        with receipt.frozen_application_patches(self.manifest) as (root, provenance):
            self.patch.write_bytes(b"substituted after validation")
            self.manifest.write_bytes(b"{}")
            self.assertEqual([entry["name"] for entry in provenance["patches"]],
                             [second["name"], self.entry["name"]])
            for entry in provenance["patches"]:
                self.assertEqual((root / entry["path"]).read_bytes(), original)
            self.assertEqual((root / "INPUT_MANIFEST.json").read_bytes(), manifest)
        self.assertFalse(root.exists())

    def test_digest_mismatch_never_calls_materialization(self):
        self.write([{**self.entry, "sha256": "0" * 64}])
        with mock.patch.object(sys, "argv", ["prepare.py", "--application-patch-manifest", str(self.manifest)]), \
                mock.patch.object(prepare, "prepare") as materialize:
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                prepare.main()
            materialize.assert_not_called()

    def test_cli_overrides_environment(self):
        with mock.patch.dict("os.environ", {"WGPU_APPLICATION_PATCH_MANIFEST": "/missing/input.json"}), \
                mock.patch.object(sys, "argv", ["prepare.py", "--application-patch-manifest", str(self.manifest)]), \
                mock.patch.object(prepare, "prepare") as materialize:
            prepare.main()
            self.assertEqual(materialize.call_args.args[2]["patches"][0]["name"], self.entry["name"])

    def test_invalid_names(self):
        for name in ("", "/absolute.patch", "../escape.patch", "a/../b.patch", "a//b.patch",
                     "./patch", "a/./b.patch", "a\\b.patch", "trailing/", "a:b.patch"):
            with self.subTest(name=name):
                self.write([{**self.entry, "name": name}])
                with self.assertRaises(ValueError), receipt.frozen_application_patches(self.manifest):
                    pass

    def test_duplicate_and_overlapping_names(self):
        for name in (self.entry["name"], "app", self.entry["name"] + "/nested.patch"):
            self.write([self.entry, {**self.entry, "name": name}])
            with self.subTest(name=name), self.assertRaises(ValueError), \
                    receipt.frozen_application_patches(self.manifest):
                pass

    def test_symlink_patch_and_directory_rejected(self):
        alias = self.root / "link.patch"
        alias.symlink_to(self.patch)
        self.write([{**self.entry, "path": str(alias)}])
        with self.assertRaisesRegex(ValueError, "Symlink"), receipt.frozen_application_patches(self.manifest):
            pass
        linked_dir = self.root / "linked"
        linked_dir.symlink_to(self.root, target_is_directory=True)
        self.write([{**self.entry, "path": str(linked_dir / self.patch.name)}])
        with self.assertRaisesRegex(ValueError, "Symlink"), receipt.frozen_application_patches(self.manifest):
            pass

    def test_nonregular_patch_rejected(self):
        self.write([{**self.entry, "path": str(self.root)}])
        with self.assertRaisesRegex(ValueError, "regular file"), receipt.frozen_application_patches(self.manifest):
            pass

    def test_manifest_duplicate_key_and_bad_version_rejected(self):
        self.manifest.write_text('{"schema_version":1,"schema_version":1,"patches":[]}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"), receipt.frozen_application_patches(self.manifest):
            pass
        for version in (True, 2, "1"):
            self.manifest.write_text(json.dumps({"schema_version": version, "patches": []}))
            with self.assertRaises(ValueError), receipt.frozen_application_patches(self.manifest):
                pass

    def test_apply_is_forward_only_and_zero_fuzz(self):
        with mock.patch.object(prepare.subprocess, "run") as run:
            prepare.apply(self.root, self.patch)
        self.assertEqual(run.call_args.args[0], ["patch", "--batch", "--forward", "--fuzz=0",
                                               "-p1", "-i", str(self.patch)])
        self.assertEqual(run.call_args.kwargs, {"cwd": self.root, "check": True})


class PreparedSource(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.foundation = self.root / "foundation"
        for name in receipt.FOUNDATION_INPUTS:
            path = self.foundation / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sources": []}' if name == "IMPORT.json" else name)
        self.out = self.root / "output"
        self.crates = {name: self.out / f"deps/{name}-{version}"
                       for name, (version, _) in receipt.PATCHED_CRATES.items()}
        for root in self.crates.values():
            (root / "src").mkdir(parents=True)
            (root / "src/lib.rs").write_text("crate fixture")
        for name in ("source/src/lib.rs", "source/Cargo.lock", "cargo.cosmo.toml", "generated/vulkan_signatures.inc"):
            path = self.out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        with receipt.frozen_application_patches() as (root, self.provenance):
            shutil.copytree(root, self.out / "application-patches")
        self.prepared = receipt.write_prepared(self.out, self.foundation, self.crates, self.provenance)
        self.checksum = receipt.file_sha256(self.out / "PREPARED.json")

    def test_roundtrip_and_digest_definition(self):
        result, checksum = receipt.verify_prepared(self.out, self.foundation, self.checksum)
        self.assertEqual(result, self.prepared)
        self.assertEqual(checksum, self.checksum)
        self.assertEqual(result["prepared_source_sha256"],
                         hashlib.sha256(receipt.encoded(result["source_trees"])).hexdigest())
        self.assertEqual(result["application_patches"], {})

    def test_changed_sources_and_generated_inputs_rejected(self):
        names = ["source/src/lib.rs", "source/Cargo.lock", "cargo.cosmo.toml", "generated/vulkan_signatures.inc"]
        names.extend((path / "src/lib.rs").relative_to(self.out).as_posix() for path in self.crates.values())
        for name in names:
            path = self.out / name
            original = path.read_bytes()
            path.write_bytes(original + b" changed")
            with self.subTest(name=name), self.assertRaises(ValueError):
                receipt.verify_prepared(self.out, self.foundation, self.checksum)
            path.write_bytes(original)

    def test_missing_or_arbitrary_crate_tree_rejected(self):
        for replacement in ({"source": self.prepared["source_trees"]["source"]},
                            {**self.prepared["source_trees"], "deps/unpinned": {}}):
            changed = {**self.prepared, "source_trees": replacement,
                       "prepared_source_sha256": receipt.sha256(receipt.encoded(replacement))}
            (self.out / "PREPARED.json").write_bytes(receipt.encoded(changed))
            with self.assertRaisesRegex(ValueError, "pinned crates"):
                receipt.verify_prepared(self.out, self.foundation)

    def test_added_source_file_rejected(self):
        (self.out / "source/new.rs").write_text("not prepared")
        with self.assertRaisesRegex(ValueError, "source changed"):
            receipt.verify_prepared(self.out, self.foundation, self.checksum)

    def test_changed_receipt_cannot_relabel_old_build(self):
        self.prepared["application_patches"] = {"app/fake.patch": "0" * 64}
        (self.out / "PREPARED.json").write_text(json.dumps(self.prepared))
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            receipt.verify_prepared(self.out, self.foundation, self.checksum)

    def test_changed_foundation_patch_rejected(self):
        (self.foundation / "patches/wgpu-native-cosmopolitan.patch").write_text("new patch")
        with self.assertRaisesRegex(ValueError, "Foundation preparation inputs changed"):
            receipt.verify_prepared(self.out, self.foundation, self.checksum)

    def test_changed_patch_provenance_rejected(self):
        (self.out / "application-patches/MANIFEST.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "provenance changed"):
            receipt.verify_prepared(self.out, self.foundation, self.checksum)

    def test_symlink_source_rejected(self):
        (self.out / "source/link.rs").symlink_to(self.out / "source/src/lib.rs")
        with self.assertRaisesRegex(ValueError, "Non-regular"):
            receipt.verify_prepared(self.out, self.foundation, self.checksum)

    def test_bootstrap_lock_does_not_authorize_build(self):
        (self.foundation / "Cargo.cosmo.lock").unlink()
        receipt.write_prepared(self.out, self.foundation, self.crates, self.provenance,
                               bootstrap_lock=True)
        with self.assertRaisesRegex(ValueError, "Bootstrap-lock"):
            receipt.verify_prepared(self.out, self.foundation)
        receipt.verify_prepared(self.out, self.foundation, allow_bootstrap=True)


class PristineInputs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "src/lib.rs").write_text("pristine native")
        (self.root / "ffi/webgpu-headers").mkdir(parents=True)
        (self.root / "ffi/webgpu-headers/webgpu.h").write_text("pristine headers")
        self.manifest = {"sources": [
            {"project": "wgpu-native", "included": [{"path": "src/lib.rs",
             "sha256": receipt.file_sha256(self.root / "src/lib.rs")}]},
            {"project": "webgpu-headers", "included": [{"path": "webgpu.h",
             "sha256": receipt.file_sha256(self.root / "ffi/webgpu-headers/webgpu.h")}]},
        ]}

    def test_exact_combined_files(self):
        receipt.verify_pristine(self.root, self.manifest)

    def test_declared_upstream_names_may_contain_spaces(self):
        path = self.root / "ffi/webgpu-headers/Asynchronous Operations.md"
        path.write_text("upstream documentation")
        self.manifest["sources"][1]["included"].append({
            "path": path.name, "sha256": receipt.file_sha256(path)})
        receipt.verify_pristine(self.root, self.manifest)

    def test_unlisted_file_cannot_be_blessed(self):
        (self.root / "src/unlisted.rs").write_text("must be supplied by explicit patch")
        with self.assertRaisesRegex(ValueError, "unlisted"):
            receipt.verify_pristine(self.root, self.manifest)

    def test_modified_and_missing_files_rejected(self):
        path = self.root / "src/lib.rs"
        path.write_text("changed")
        with self.assertRaises(ValueError):
            receipt.verify_pristine(self.root, self.manifest)
        path.unlink()
        with self.assertRaises(ValueError):
            receipt.verify_pristine(self.root, self.manifest)

    def test_pristine_symlink_rejected(self):
        (self.root / "src/link.rs").symlink_to(self.root / "src/lib.rs")
        with self.assertRaisesRegex(ValueError, "Non-regular"):
            receipt.verify_pristine(self.root, self.manifest)


if __name__ == "__main__":
    unittest.main()
