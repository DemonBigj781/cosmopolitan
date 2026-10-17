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

    def check(self, failure=None):
        def run(command, **kwargs):
            text = "PASS"
            if command[-1] == "version": text = "ape-toolchain bootstrap 0.1"
            if command[-1] == "status": text = "Resource access: ZIPOS"
            if "--full" in command: text = "int main(int argc, char** argv) {return 0;}"
            if command[-1] == "--version": text = "compiler version"
            kwargs["stdout"].write(text + "\n")
            return subprocess.CompletedProcess(command, 0)
        def probe(binary, language, output, loader):
            return {"language": language, "passed": language != failure}
        with patch("compiler_hosts.host_platform", return_value=("Linux", "x86_64")), \
             patch("compiler_hosts.prepare_loader", return_value=Path("/loader")), \
             patch("compiler_hosts.validate_bundle"), \
             patch("compiler_hosts.subprocess.run", side_effect=run), \
             patch("compiler_hosts.probe", side_effect=probe) as mocked:
            result = check_host(self.binary, self.root / "BUILD.json", self.root,
                                self.root / "results", "1" * 40, "Linux", "x86_64")
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


if __name__ == "__main__":
    unittest.main()
