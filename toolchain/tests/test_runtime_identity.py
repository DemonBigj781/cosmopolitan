"""Deferred behavioral coverage. Run only when CPU tests are authorized."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_identity import (ARCHITECTURES, MANIFEST, SOURCE_INPUTS, digest_file,
                              inventory, required_files, runtime_identity,
                              selected_runtime, source_inventory_sha256,
                              verify_runtime_sdk)


class RuntimeIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in required_files():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture bytes, not a real SDK")
        (self.root / "bin/cc").symlink_to("cosmocc")
        self.record = {
            "schema_version": 1, "kind": "source-built-cosmopolitan-runtime",
            "runtime_abi": "cosmopolitan-source-runtime-v1",
            "architectures": ARCHITECTURES,
            "source": {"repository": "https://example.invalid/source.git",
                       "revision": "a" * 40, "tree": "b" * 40, "clean": True},
            "bootstrap": {"role": "stock-compiler-input-only",
                          "tree_sha256": "c" * 64, "archive_sha256": "d" * 64},
            "source_abi_inputs": {name: "e" * 64 for name in (
                "libc/nt/struct/sid.h", "libc/dlopen/dladdr.c",
                "libc/intrin/fds.h", "libc/proc/posix_spawn.c")},
            "producer": {"entrypoint": "tool/cosmocc/package.sh", "fresh_outputs": True},
            "files": inventory(self.root),
        }
        self.write_manifest()

    def write_manifest(self):
        self.record["runtime_identity"] = runtime_identity(self.record)
        (self.root / MANIFEST).write_text(json.dumps(self.record))

    def add_source_inputs(self):
        files = {name: {"mode": "100644", "type": "file", "size": 17, "sha256": digest}
                 for name, digest in self.record["source_abi_inputs"].items()}
        recipe = "tool/cosmocc/package.sh"
        self.record["producer"]["sha256"] = "f" * 64
        files[recipe] = {"mode": "100755", "type": "file", "size": 29, "sha256": "f" * 64}
        target = "libc/nt/struct/sid.h"
        files["source-link"] = {"mode": "120000", "type": "symlink", "size": len(target),
                                "target": target, "sha256": hashlib.sha256(target.encode()).hexdigest()}
        inputs = {"schema_version": 1, "kind": "cosmopolitan-tracked-source-inputs",
                  "source": copy.deepcopy(self.record["source"]), "files": files}
        self.record["source_patches"] = []
        self.write_source_inputs(inputs)
        return inputs

    def write_source_inputs(self, inputs):
        path = self.root / SOURCE_INPUTS
        path.write_text(json.dumps(inputs))
        self.record["source_tree_sha256"] = source_inventory_sha256(inputs["files"])
        self.record["source_inventory_record"] = {"path": SOURCE_INPUTS, "sha256": digest_file(path)}
        self.record["files"] = inventory(self.root, exclude=(MANIFEST,))
        self.write_manifest()

    def test_complete_source_evidence_is_bound_without_original_checkout(self):
        inputs = self.add_source_inputs()
        actual = verify_runtime_sdk(self.root)
        self.assertEqual(actual["source_tree_sha256"], source_inventory_sha256(inputs["files"]))
        self.assertEqual(actual["files"][SOURCE_INPUTS]["sha256"],
                         actual["source_inventory_record"]["sha256"])
        self.assertNotEqual(actual["source_tree_sha256"],
                            hashlib.sha256(actual["source"]["tree"].encode()).hexdigest())

    def test_partial_source_evidence_is_never_legacy(self):
        self.add_source_inputs()
        complete = copy.deepcopy(self.record)
        for field in ("source_tree_sha256", "source_inventory_record", "source_patches"):
            with self.subTest(missing=field):
                self.record = copy.deepcopy(complete)
                del self.record[field]
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, "Incomplete source input"):
                    verify_runtime_sdk(self.root)

    def test_orphaned_source_record_is_not_legacy(self):
        (self.root / SOURCE_INPUTS).write_text("{}")
        self.record["files"] = inventory(self.root, exclude=(MANIFEST,))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Unbound source input"):
            verify_runtime_sdk(self.root)

    def test_source_record_raw_bytes_are_preserved_and_bound(self):
        inputs = self.add_source_inputs()
        path = self.root / SOURCE_INPUTS
        path.write_text(json.dumps(inputs, indent=2))
        self.record["files"] = inventory(self.root, exclude=(MANIFEST,))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "record digest mismatch"):
            verify_runtime_sdk(self.root)

    def test_source_record_must_be_regular_and_present(self):
        self.add_source_inputs()
        path = self.root / SOURCE_INPUTS
        path.rename(self.root / "source-input-copy")
        with self.assertRaisesRegex(ValueError, "regular SOURCE_INPUTS"):
            verify_runtime_sdk(self.root)
        path.symlink_to("source-input-copy")
        self.record["files"] = inventory(self.root, exclude=(MANIFEST,))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "regular SOURCE_INPUTS"):
            verify_runtime_sdk(self.root)

    def test_source_record_identity_matches_source_manifest(self):
        inputs = self.add_source_inputs()
        inputs["source"]["revision"] = "0" * 40
        self.write_source_inputs(inputs)
        with self.assertRaisesRegex(ValueError, "inventory identity mismatch"):
            verify_runtime_sdk(self.root)

    def test_source_digest_is_complete_inventory_not_git_id_hash(self):
        self.add_source_inputs()
        self.record["source_tree_sha256"] = hashlib.sha256(self.record["source"]["tree"].encode()).hexdigest()
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Complete source inventory digest mismatch"):
            verify_runtime_sdk(self.root)

    def test_record_reference_is_fixed_and_exact(self):
        self.add_source_inputs()
        for reference in ({"path": "../SOURCE_INPUTS.json", "sha256": "a" * 64},
                          {"path": SOURCE_INPUTS, "sha256": "not a digest"},
                          {"path": SOURCE_INPUTS, "sha256": "a" * 64, "extra": True}):
            with self.subTest(reference=reference):
                self.record["source_inventory_record"] = reference
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, "Invalid source input inventory reference"):
                    verify_runtime_sdk(self.root)

    def test_source_patches_mean_no_patches_after_pinned_commit(self):
        self.add_source_inputs()
        self.record["source_patches"] = ["unrecorded.patch"]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "no patches after the pinned commit"):
            verify_runtime_sdk(self.root)

    def test_source_inventory_abi_and_producer_must_agree(self):
        original = self.add_source_inputs()
        for name in ("libc/nt/struct/sid.h", "tool/cosmocc/package.sh"):
            with self.subTest(name=name):
                inputs = copy.deepcopy(original)
                inputs["files"][name]["sha256"] = "0" * 64
                self.write_source_inputs(inputs)
                with self.assertRaisesRegex(ValueError, "fingerprint differs from complete"):
                    verify_runtime_sdk(self.root)

    def test_source_inventory_rejects_bad_paths_and_metadata(self):
        original = self.add_source_inputs()
        for name, entry in (
                ("../escape", {"mode": "100644", "type": "file", "size": 1, "sha256": "a" * 64}),
                ("libc/nt/struct/sid.h/child", {"mode": "100644", "type": "file", "size": 1, "sha256": "a" * 64}),
                ("bad-mode", {"mode": "160000", "type": "file", "size": 1, "sha256": "a" * 64}),
                ("bad-size", {"mode": "100644", "type": "file", "size": True, "sha256": "a" * 64}),
                ("bad-type", {"mode": "100644", "type": "directory", "size": 1, "sha256": "a" * 64}),
                ("bad-link", {"mode": "120000", "type": "symlink", "size": 4,
                              "target": "else", "sha256": "a" * 64})):
            with self.subTest(name=name):
                inputs = copy.deepcopy(original)
                inputs["files"][name] = entry
                self.write_source_inputs(inputs)
                with self.assertRaises(ValueError):
                    verify_runtime_sdk(self.root)

    def test_complete_inventory_retains_internal_alias(self):
        actual = verify_runtime_sdk(self.root)
        self.assertEqual(actual["files"]["bin/cc"]["target"], "cosmocc")

    def test_changed_missing_and_extra_files(self):
        path = self.root / "include/stdio.h"
        path.write_text("mixed runtime")
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)
        path.unlink()
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)
        path.write_bytes(b"fixture bytes, not a real SDK")
        (self.root / "foreign.a").write_bytes(b"extra")
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)

    def test_escape_and_cycle_links(self):
        link = self.root / "bin/cc"
        for target in ("../../outside", "/etc/passwd", "cc"):
            link.unlink()
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                inventory(self.root)

    def test_manifest_tamper(self):
        self.record["source"]["revision"] = "f" * 40
        (self.root / MANIFEST).write_text(json.dumps(self.record))
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)

    def test_unknown_architecture_even_with_rehashed_manifest(self):
        self.record["architectures"] = ["riscv64"]
        self.write_manifest()
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)

    def test_missing_required_runtime_even_with_rehashed_manifest(self):
        (self.root / "x86_64-linux-cosmo/lib/libcxx.a").unlink()
        self.record["files"] = inventory(self.root, exclude=(MANIFEST,))
        self.write_manifest()
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)

    def test_mode_change(self):
        path = self.root / "bin/cosmocc"
        path.chmod(path.stat().st_mode ^ 0o100)
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)

    def test_manifest_symlink(self):
        (self.root / "record").write_text((self.root / MANIFEST).read_text())
        (self.root / MANIFEST).unlink()
        (self.root / MANIFEST).symlink_to("record")
        with self.assertRaises(ValueError):
            verify_runtime_sdk(self.root)


    def test_selected_runtime_defaults_to_legacy_stock(self):
        (self.root / MANIFEST).unlink()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(selected_runtime(self.root), {"kind": "stock-sdk"})
        self.assertEqual(selected_runtime(self.root, "stock-sdk"), {"kind": "stock-sdk"})

    def test_selected_stock_rejects_source_manifest(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "explicit source-built"):
                selected_runtime(self.root)
        with self.assertRaisesRegex(ValueError, "explicit source-built"):
            selected_runtime(self.root, "stock-sdk")

    def test_selected_stock_rejects_dangling_source_manifest(self):
        (self.root / MANIFEST).unlink()
        (self.root / MANIFEST).symlink_to("missing-source-record")
        with self.assertRaisesRegex(ValueError, "explicit source-built"):
            selected_runtime(self.root, "stock-sdk")

    def test_selected_source_returns_compact_verified_summary(self):
        expected = {key: self.record[key] for key in (
            "kind", "source", "runtime_identity", "runtime_abi")}
        self.assertEqual(selected_runtime(self.root, "source-built"), expected)
        with patch.dict(os.environ, {"COSMO_RUNTIME_PROFILE": "source-built"}):
            self.assertEqual(selected_runtime(self.root), expected)
        (self.root / "include/stdio.h").write_text("changed runtime")
        with self.assertRaises(ValueError):
            selected_runtime(self.root, "source-built")

    def test_selected_runtime_rejects_unknown_profile(self):
        for profile in ("", "source", "stock", "source-built "):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, "must be stock-sdk or source-built"):
                    selected_runtime(self.root, profile)
        with patch.dict(os.environ, {"COSMO_RUNTIME_PROFILE": "unknown"}):
            with self.assertRaises(ValueError):
                selected_runtime(self.root)

    def test_selected_explicit_profile_overrides_environment(self):
        with patch.dict(os.environ, {"COSMO_RUNTIME_PROFILE": "stock-sdk"}):
            summary = selected_runtime(self.root, "source-built")
            self.assertEqual(summary["runtime_identity"], self.record["runtime_identity"])


if __name__ == "__main__":
    unittest.main()
