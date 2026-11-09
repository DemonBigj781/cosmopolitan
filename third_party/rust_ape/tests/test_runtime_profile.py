"""Deferred source-profile behavioral coverage; do not run before the CPU gate."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_profile
from stage import verify_shims


class RuntimeProfileTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / "rust-sdk"
        self.destination = self.sdk / "vendor/cosmocc"
        self.destination.mkdir(parents=True)
        (self.destination / "bootstrap").write_text("original compiler")
        self.runtime = self.root / "source-sdk"
        self.runtime.mkdir()
        (self.runtime / "source").write_text("whole source runtime")
        self.profile = {"runtime_abi": "cosmopolitan-source-runtime-v1",
                        "source_abi_inputs": {"source": "reviewed"}}
        self.record = {**self.profile, "runtime_identity": "sha256:" + "a" * 64}
        for name, value in (("require_profile", self.profile), ("verify_runtime_sdk", self.record)):
            mocked = patch.object(runtime_profile, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_binding_publication_failure_restores_bootstrap(self):
        with patch.object(runtime_profile, "atomic_json", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                runtime_profile.activate(self.sdk, self.runtime)
        self.assertEqual((self.destination / "bootstrap").read_text(), "original compiler")
        self.assertFalse((self.destination / "source").exists())
        self.assertFalse((self.sdk / runtime_profile.BINDING).exists())

    def test_activation_is_idempotent_and_tables_can_be_completed_later(self):
        runtime_profile.activate(self.sdk, self.runtime)
        runtime_profile.activate(self.sdk, self.runtime)
        self.assertTrue((self.destination / "source").is_file())
        for name in runtime_profile.TABLES:
            path = self.sdk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated constants")
        runtime_profile.record_tables(self.sdk)
        record = runtime_profile.verify_binding(self.sdk)
        self.assertEqual(set(record["generated_tables"]), set(runtime_profile.TABLES))
        (self.sdk / runtime_profile.TABLES[0]).write_text("changed constants")
        with self.assertRaises(ValueError):
            runtime_profile.verify_binding(self.sdk)
        runtime_profile.record_tables(self.sdk)
        runtime_profile.verify_binding(self.sdk)

    def test_mismatched_abi_cannot_activate(self):
        with patch.object(runtime_profile, "verify_runtime_sdk", return_value={
                **self.record, "source_abi_inputs": {"source": "changed"}}):
            with self.assertRaises(ValueError):
                runtime_profile.activate(self.sdk, self.runtime)
        self.assertTrue((self.destination / "bootstrap").is_file())

    def test_existing_objects_cannot_cross_runtime_boundary(self):
        generated = self.sdk / "generated"
        generated.mkdir()
        (generated / "shim-old-x86_64.o").write_bytes(b"stale stock object")
        with self.assertRaises(ValueError):
            runtime_profile.activate(self.sdk, self.runtime)


class AdapterIntegrityTest(unittest.TestCase):
    def test_object_closure_rejects_stale_extras_and_name_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            generated = sdk / "generated"
            generated.mkdir()
            (generated / "shim-close-x86_64.o").write_bytes(b"object")
            profile = {"shim_sources_sha256": {"shim/close.c": "source hash"}}
            with patch.object(runtime_profile, "require_profile", return_value=profile):
                self.assertEqual(len(runtime_profile.expected_shim_objects(sdk)), 1)
                (generated / "shim-dlmalloc-x86_64.o").write_bytes(b"stale")
                with self.assertRaises(ValueError):
                    runtime_profile.expected_shim_objects(sdk)
            collision = {"shim_sources_sha256": {"shim/a/b.c": "x", "shim/a-b.c": "y"}}
            with patch.object(runtime_profile, "require_profile", return_value=collision):
                with self.assertRaises(ValueError):
                    runtime_profile.expected_shim_objects(sdk)

    def test_exact_shim_set_and_wrapper_but_mutable_generated_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            expected = {}
            for name in ("shim/close.c", "shim/path.h", "scripts/gcc-linker-wrapper.bash"):
                path = sdk / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
                expected[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            metadata = {"shim_sources_sha256": expected}
            (sdk / "shim/tables.h").write_text("generated separately")
            verify_shims(sdk, metadata)
            (sdk / "shim/dlmalloc.c").write_text("removed stock override")
            with self.assertRaises(RuntimeError):
                verify_shims(sdk, metadata)
            (sdk / "shim/dlmalloc.c").unlink()
            (sdk / "scripts/gcc-linker-wrapper.bash").write_text("different wrapper")
            with self.assertRaises(RuntimeError):
                verify_shims(sdk, metadata)


if __name__ == "__main__":
    unittest.main()
