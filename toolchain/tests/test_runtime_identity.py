"""Deferred behavioral coverage. Run only when CPU tests are authorized."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_identity import (ARCHITECTURES, MANIFEST, inventory, required_files,
                              runtime_identity, selected_runtime, verify_runtime_sdk)


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
