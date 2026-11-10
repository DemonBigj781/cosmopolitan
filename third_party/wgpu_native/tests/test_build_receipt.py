#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fixture-only build evidence checks. This file does not invoke any compiler."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_receipt
from source_receipt import file_sha256


class CargoEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.out = self.repo / "o/webgpu"
        self.sdk = self.repo / "o/rust-ape/sdk"
        self.archive = self.repo / "o/rust-ape/build/x86_64-unknown-linux-musl/release/libwgpu_native.a"
        self.wrapper = self.repo / "third_party/rust_ape/run.sh"
        self.target = self.sdk / "generated/x86_64-unknown-linux-musl.json"
        self.config = self.out / "cargo.cosmo.toml"
        for path in (self.archive, self.wrapper, self.target, self.config, self.out / "source/Cargo.toml"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture bytes, not a compiled artifact")
        self.runtime = {"kind": "source-built", "fixture": "exact runtime identity"}
        self.recipe = {
            "schema_version": 1, "status": "invocation-only", "runtime": self.runtime,
            "argv": ["cargo", "build", "--locked"],
            "wrapper_sha256": file_sha256(self.wrapper),
            "config_sha256": {str(self.config): file_sha256(self.config)},
            "target_sha256": file_sha256(self.target),
            "environment": {"CARGO_BUILD_TARGET": str(self.target)},
        }
        self.invocation = self.out / "cargo-invocation.json"
        self.invocation.write_text(json.dumps(self.recipe))
        self.artifact = {"reason": "compiler-artifact", "manifest_path": str(self.out / "source/Cargo.toml"),
                         "target": {"name": "wgpu_native", "crate_types": ["staticlib"]},
                         "filenames": [str(self.archive)], "fresh": True}
        self.log = self.out / "cargo-artifacts.jsonl"
        self.write_log([self.artifact, {"reason": "build-finished", "success": True}])

    def write_log(self, entries):
        self.log.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    def verify(self, **kwargs):
        return build_receipt.verify_cargo(self.out, self.sdk, self.runtime, self.archive, **kwargs)

    def test_successful_selected_staticlib_and_cache_hit(self):
        result = self.verify()
        self.assertEqual(result["selected_staticlib"], str(self.archive))
        self.assertEqual(result["cargo_invocation_sha256"], file_sha256(self.invocation))
        self.assertEqual(result["cargo_artifacts_sha256"], file_sha256(self.log))
        self.assertEqual(result["cargo_invocation"]["status"], "invocation-only")

    def test_failed_or_missing_build_is_not_success(self):
        for entries in ([self.artifact], [self.artifact, {"reason": "build-finished", "success": False}],
                        [{"reason": "build-finished", "success": True}]):
            self.write_log(entries)
            with self.assertRaisesRegex(ValueError, "successful build"):
                self.verify()

    def test_stale_or_unrelated_staticlib_is_not_accepted(self):
        for replacement in ({"filenames": [str(self.archive.with_name("other.a"))]},
                            {"manifest_path": str(self.repo / "unrelated/Cargo.toml")},
                            {"target": {"name": "other", "crate_types": ["staticlib"]}},
                            {"target": {"name": "wgpu_native", "crate_types": ["rlib"]}}):
            self.write_log([{**self.artifact, **replacement}, {"reason": "build-finished", "success": True}])
            with self.assertRaisesRegex(ValueError, "successful build"):
                self.verify()

    def test_changed_invocation_or_log_after_cargo_rejected(self):
        before_invocation, before_log = file_sha256(self.invocation), file_sha256(self.log)
        self.invocation.write_text(self.invocation.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "invocation receipt changed"):
            self.verify(expected_invocation_sha256=before_invocation)
        self.log.write_text(self.log.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "artifact log changed"):
            self.verify(expected_artifacts_sha256=before_log)

    def test_runtime_and_target_and_config_changes_rejected(self):
        self.recipe["runtime"] = {"kind": "stock-sdk"}
        self.invocation.write_text(json.dumps(self.recipe))
        with self.assertRaisesRegex(ValueError, "selected runtime"):
            self.verify()
        self.recipe["runtime"] = self.runtime
        self.invocation.write_text(json.dumps(self.recipe))
        self.target.write_text("changed")
        with self.assertRaisesRegex(ValueError, "selected SDK"):
            self.verify()

    def test_recipe_records_exact_arguments_without_shell_reparsing(self):
        path = self.out / "commands.json"
        argv = ["path with spaces/linker", "-DNAME=a b", "", "-o", "output name"]
        build_receipt.record_command(path, "link", argv)
        self.assertEqual(json.loads(path.read_text())["link"]["argv"], argv)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            build_receipt.record_command(path, "link", argv)


if __name__ == "__main__":
    unittest.main()
