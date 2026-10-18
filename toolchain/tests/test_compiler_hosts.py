"""Unit coverage for independent compiler-host reporting, not portability proof."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from compiler_hosts import check_host
from file_metadata import sha256
from smoke_compilers import PROBES
from bootstrap import RUST_HOSTS


class CompilerHostsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "toolchain.com"
        self.binary.write_bytes(b"fixture")
        (self.root / "BUILD.json").write_text(json.dumps({
            "source_commit": "1" * 40, "artifact": {"sha256": sha256(self.binary)},
            "inputs": {"rust_compiler": {"hosts": list(RUST_HOSTS)}}}))

    def check(self, failure=None, system="Linux", arch="x86_64", unsupported_message=None):
        def run(command, **kwargs):
            if unsupported_message and "rust" in command and command[-1] == "--version":
                kwargs["stderr"].write(unsupported_message)
                return subprocess.CompletedProcess(command, 126)
            text = "PASS"
            if command[-1] == "version": text = "ape-toolchain bootstrap 0.1"
            if command[-1] == "status": text = "Resource access: ZIPOS"
            if "--full" in command: text = "int main(int argc, char** argv) {return 0;}"
            if command[-1] == "--version": text = "compiler version"
            kwargs["stdout"].write(text + "\n")
            return subprocess.CompletedProcess(command, 0)
        def probe(binary, language, output, loader):
            return {"language": language, "passed": language != failure}
        with patch("compiler_hosts.host_platform", return_value=(system, arch)), \
             patch("compiler_hosts.prepare_loader", return_value=Path("/loader")), \
             patch("compiler_hosts.validate_bundle"), \
             patch("compiler_hosts.check_cargo_project", return_value={"passed": True}), \
             patch("compiler_hosts.subprocess.run", side_effect=run), \
             patch("compiler_hosts.probe", side_effect=probe) as mocked:
            result = check_host(self.binary, self.root / "BUILD.json", self.root,
                                self.root / "results", "1" * 40, system, arch)
        return result, mocked

    def test_every_backend_is_tested_even_after_failure(self):
        result, calls = self.check("c")
        self.assertFalse(result["passed"])
        self.assertEqual(calls.call_count, len(PROBES))
        self.assertEqual([p["language"] for p in result["compilers"]], list(PROBES))

    def test_hash_mismatch_fails_before_any_compiler(self):
        self.binary.write_bytes(b"changed")
        result, calls = self.check()
        self.assertFalse(result["passed"])
        self.assertIn("hash", result["error"])
        calls.assert_not_called()

    def test_launch_and_compile_results_are_separate(self):
        result, _ = self.check()
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["launcher"]), 7)
        self.assertEqual(len(result["compilers"]), 4)

    def test_macos_rust_must_build_successfully_now(self):
        result, calls = self.check(system="Darwin", arch="aarch64")
        self.assertTrue(result["passed"])
        self.assertEqual(calls.call_count, 4)
        rust = next(p for p in result["compilers"] if p["language"] == "rust")
        self.assertTrue(rust["passed"])
        self.assertTrue(rust["supported"])
        self.assertNotIn("rejection_validated", rust)
        self.assertEqual(result["rust_compiler_host"], "aarch64-apple-darwin")

    def test_other_errors_are_not_disguised_as_expected_rust_rejections(self):
        result, _ = self.check(system="Darwin", unsupported_message="unexpected compiler crash")
        self.assertFalse(result["passed"])

    def test_old_unsupported_rust_message_is_no_longer_a_success(self):
        result, _ = self.check(system="Darwin", unsupported_message=
            "Unsupported packaged Rust host: requires Linux x86-64; no SDK extracted")
        self.assertFalse(result["passed"])

    def test_missing_host_payload_fails_before_compilers(self):
        manifest = json.loads((self.root / "BUILD.json").read_text())
        manifest["inputs"]["rust_compiler"]["hosts"] = [RUST_HOSTS[0]]
        (self.root / "BUILD.json").write_text(json.dumps(manifest))
        result, calls = self.check(system="Darwin", arch="aarch64")
        self.assertFalse(result["passed"])
        calls.assert_not_called()


class CargoHostTest(unittest.TestCase):
    def test_real_metadata_is_required_in_addition_to_a_zero_exit(self):
        from compiler_hosts import check_cargo_project
        for produce_metadata in (False, True):
            with self.subTest(produce_metadata=produce_metadata), tempfile.TemporaryDirectory() as temporary:
                def run(command, **kwargs):
                    self.assertIn("--offline", command)
                    self.assertNotIn("RUSTC_WRAPPER", kwargs["env"])
                    kwargs["stdout"].write("SDK materialized: Rust (fixture)\n")
                    if produce_metadata:
                        target = Path(command[command.index("--target-dir") + 1]) / "debug/deps"
                        target.mkdir(parents=True)
                        (target / "libcargo_host_probe-fixture.rmeta").write_bytes(b"metadata")
                    return subprocess.CompletedProcess(command, 0)
                with patch("compiler_hosts.subprocess.run", side_effect=run):
                    result = check_cargo_project(Path("/toolchain"), Path(temporary), Path("/loader"))
                self.assertEqual(result["passed"], produce_metadata)


if __name__ == "__main__":
    unittest.main()
