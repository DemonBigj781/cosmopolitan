"""Test smoke-runner reporting; these mocks do not validate a real compiler."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from smoke_compilers import PROBES, probe


class SmokeRunnerTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.output = Path(self.workspace.name)

    def run_probe(self, *, build_exit=0, run_exit=0, output=None,
                  header=b"MZqFpD='", produce_binary=True, loader=None,
                  components=("C",), leak=False):
        def run(command, **kwargs):
            if "-o" in command:
                for component in components:
                    kwargs["stdout"].write(f"SDK materialized: {component} (test fixture)\n")
                if leak:
                    (Path(kwargs["env"]["TMPDIR"]) / "cosmo-sdk-leak").mkdir()
                if produce_binary and build_exit == 0:
                    Path(command[-1]).write_bytes(header)
                return subprocess.CompletedProcess(command, build_exit)
            kwargs["stdout"].write(PROBES["c"][2] if output is None else output)
            return subprocess.CompletedProcess(command, run_exit)

        with patch("smoke_compilers.subprocess.run", side_effect=run) as mocked:
            result = probe(Path("/toolchain.com"), "c", self.output, loader)
        return result, mocked

    def test_success_requires_compilation_and_execution(self):
        result, mocked = self.run_probe()
        self.assertTrue(result["passed"])
        self.assertEqual(mocked.call_count, 2)

    def test_compile_failure_does_not_run_program(self):
        result, mocked = self.run_probe(build_exit=1)
        self.assertFalse(result["passed"])
        self.assertEqual(mocked.call_count, 1)

    def test_missing_binary_is_failure(self):
        result, mocked = self.run_probe(produce_binary=False)
        self.assertFalse(result["passed"])
        self.assertEqual(mocked.call_count, 1)

    def test_host_elf_output_is_not_accepted_as_ape(self):
        result, mocked = self.run_probe(header=b"\x7fELFxxxx")
        self.assertFalse(result["passed"])
        self.assertEqual(mocked.call_count, 1)

    def test_incorrect_output_is_failure(self):
        result, _ = self.run_probe(output="wrong\n")
        self.assertFalse(result["passed"])

    def test_nonzero_runtime_exit_is_failure(self):
        result, _ = self.run_probe(run_exit=1)
        self.assertFalse(result["passed"])

    def test_explicit_loader_applies_to_toolchain_and_output(self):
        result, mocked = self.run_probe(loader=Path("/ape"))
        self.assertTrue(result["passed"])
        for call in mocked.call_args_list:
            self.assertEqual(call.args[0][0], "/ape")

    def test_existing_evidence_is_not_overwritten(self):
        self.run_probe()
        with self.assertRaises(FileExistsError):
            self.run_probe()

    def test_repeated_component_materialization_is_failure(self):
        result, _ = self.run_probe(components=("C", "C"))
        self.assertFalse(result["passed"])

    def test_owned_temporary_directory_leak_is_failure(self):
        result, _ = self.run_probe(leak=True)
        self.assertFalse(result["passed"])
        self.assertEqual(result["remaining_owned_temp_paths"], ["cosmo-sdk-leak"])


if __name__ == "__main__":
    unittest.main()
