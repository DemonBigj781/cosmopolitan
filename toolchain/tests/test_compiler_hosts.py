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


class CompilerHostsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "toolchain.com"
        self.binary.write_bytes(b"fixture")
        (self.root / "BUILD.json").write_text(json.dumps({
            "source_commit": "1" * 40, "artifact": {"sha256": sha256(self.binary)}}))

    def check(self, failure=None, system="Linux", arch="x86_64", unsupported_message=None):
        rust_supported = (system, arch) == ("Linux", "x86_64")
        rejection = ("Unsupported packaged Rust host: requires Linux x86-64; no SDK extracted\n"
                     if unsupported_message is None else unsupported_message)
        def run(command, **kwargs):
            if not rust_supported and "rust" in command and command[-1] == "--version":
                kwargs["stderr"].write(rejection)
                return subprocess.CompletedProcess(command, 126)
            text = "PASS"
            if command[-1] == "version": text = "ape-toolchain bootstrap 0.1"
            if command[-1] == "status": text = "Resource access: ZIPOS"
            if "--full" in command: text = "int main(int argc, char** argv) {return 0;}"
            if command[-1] == "--version": text = "compiler version"
            kwargs["stdout"].write(text + "\n")
            return subprocess.CompletedProcess(command, 0)
        def probe(binary, language, output, loader):
            if language == "rust" and not rust_supported:
                (output / language).mkdir()
                (output / language / "build.log").write_text(rejection)
                return {"language": language, "passed": False, "build_exit": 126,
                        "materialized_components": [], "remaining_owned_temp_paths": []}
            return {"language": language, "passed": language != failure}
        with patch("compiler_hosts.host_platform", return_value=(system, arch)), \
             patch("compiler_hosts.prepare_loader", return_value=Path("/loader")), \
             patch("compiler_hosts.validate_bundle"), \
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

    def test_known_unsupported_host_is_tested_without_claiming_rust_build_success(self):
        result, calls = self.check(system="Darwin", arch="aarch64")
        self.assertTrue(result["passed"])
        self.assertEqual(calls.call_count, 4)
        rust = next(p for p in result["compilers"] if p["language"] == "rust")
        self.assertFalse(rust["passed"])
        self.assertFalse(rust["supported"])
        self.assertTrue(rust["rejection_validated"])

    def test_other_errors_are_not_disguised_as_expected_rust_rejections(self):
        result, _ = self.check(system="Darwin", unsupported_message="unexpected compiler crash")
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
