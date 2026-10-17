"""Harness tests are not evidence of execution on the mocked operating systems."""

import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_metadata import sha256
from program_matrix import LOADERS, create_bundle, run_suite
from smoke_compilers import PROBES


def ape_fixture():
    data = bytearray(256)
    data[:8] = b"MZqFpD='"
    struct.pack_into("<I", data, 0x3c, 128)
    data[128:134] = b"PE\0\0\x64\x86"
    return bytes(data)


class ProgramMatrixTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.smoke = self.root / "smoke"
        self.rust = self.root / "rust"
        self.loaders = self.root / "loaders"
        self.bundle = self.root / "bundle"
        self.results = self.root / "results"
        self.commit = "1" * 40
        self.rust.mkdir()
        self.loaders.mkdir()
        for language in PROBES:
            directory = self.smoke / f"compiler-smoke-{language}"
            (directory / language).mkdir(parents=True)
            (directory / language / "hello.com").write_bytes(ape_fixture())
            (directory / "summary.json").write_text(json.dumps([
                {"language": language, "passed": True, "build_exit": 0, "run_exit": 0}]))
        programs = []
        for name in ("rust-std.com", "rust-threads.com"):
            file = self.rust / name
            file.write_bytes(ape_fixture())
            programs.append({"file": name, "sha256": sha256(file)})
        (self.rust / "BUILD.json").write_text(json.dumps({"source_commit": self.commit, "programs": programs}))
        (self.rust / "LICENSE.rust-ape").write_text("Rust-APE license")
        for name in LOADERS:
            (self.loaders / name).write_bytes(b"loader fixture")
        (self.loaders / "LICENSE.machin").write_text("Machin license")
        (self.loaders / "BUILD.json").write_text(json.dumps({"source_commit": self.commit}))

    def prepare(self):
        create_bundle(self.smoke, self.rust, self.loaders, self.bundle, self.commit)

    def mock_run(self, command, **kwargs):
        if command[0] == "cc":
            Path(command[command.index("-o") + 1]).write_bytes(b"native loader")
        else:
            name = Path(command[-1]).stem
            text = "threads ok\n" if name == "rust-threads" else "RUST RELOCATED STD PASS: 42\n"
            for language, (_, _, expected) in PROBES.items():
                if name == language + "-smoke":
                    text = expected
            kwargs["stdout"].write(text)
        return subprocess.CompletedProcess(command, 0)

    def run_mocked(self, system="Linux", arch="aarch64", side_effect=None, expected_arch=None):
        with patch("program_matrix.host_platform", return_value=(system, arch)), \
             patch("program_matrix.subprocess.run", side_effect=side_effect or self.mock_run) as run:
            result = run_suite(self.bundle, self.results, self.commit, system, expected_arch or arch)
        return result, run

    def test_bundle_reuses_all_six_programs_and_records_actual_hashes(self):
        self.prepare()
        record = json.loads((self.bundle / "BUILD.json").read_text())
        self.assertEqual(len(record["programs"]), 6)
        for program in record["programs"]:
            self.assertEqual(program["sha256"], sha256(self.bundle / program["file"]))
            self.assertEqual(program["windows_machine"], "x86_64")
        self.assertEqual((self.bundle / "LICENSE.machin").read_text(), "Machin license")

    def test_bundle_rejects_mismatched_rust_source_commit(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            create_bundle(self.smoke, self.rust, self.loaders, self.bundle, "2" * 40)
        self.assertFalse(self.bundle.exists())

    def test_bundle_rejects_failed_compiler_probe(self):
        (self.smoke / "compiler-smoke-c/summary.json").write_text('[{"language":"c","passed":false}]')
        with self.assertRaisesRegex(ValueError, "compiler probe"):
            self.prepare()

    def test_bundle_rejects_changed_rust_program(self):
        (self.rust / "rust-std.com").write_bytes(ape_fixture() + b"changed")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.prepare()

    def test_bundle_refuses_existing_output(self):
        self.bundle.mkdir()
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_linux_arm_uses_arm_loader_for_every_program(self):
        self.prepare()
        result, run = self.run_mocked()
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["programs"]), 6)
        self.assertEqual(run.call_count, 6)
        for call in run.call_args_list:
            self.assertEqual(Path(call.args[0][0]).name, "ape-aarch64.elf")
        self.assertTrue(all(p["execution"] == "native" for p in result["programs"]))

    def test_windows_arm_labels_x64_emulation_and_copies_unchanged_executables(self):
        self.prepare()
        result, run = self.run_mocked("Windows", "aarch64")
        self.assertTrue(result["passed"])
        for call in run.call_args_list:
            self.assertEqual(len(call.args[0]), 1)
            self.assertEqual(Path(call.args[0][0]).suffix, ".exe")
        self.assertTrue(all(p["execution"] == "x64-emulated" for p in result["programs"]))
        for program in result["programs"]:
            self.assertEqual(program["sha256_after"], program["sha256_before"])

    def test_macos_arm_builds_only_the_native_loader(self):
        self.prepare()
        result, run = self.run_mocked("Darwin", "aarch64")
        self.assertTrue(result["passed"])
        self.assertEqual(run.call_count, 7)
        compile_command = run.call_args_list[0].args[0]
        self.assertEqual(compile_command[:4], ["cc", "-O", "-arch", "arm64"])
        for call in run.call_args_list[1:]:
            self.assertEqual(Path(call.args[0][0]).name, "ape-arm64")

    def test_macos_intel_uses_bundled_macho_loader_without_compiling(self):
        self.prepare()
        result, run = self.run_mocked("Darwin", "x86_64")
        self.assertTrue(result["passed"])
        self.assertEqual(run.call_count, 6)
        self.assertEqual(Path(run.call_args.args[0][0]).name, "ape-x86_64.macho")

    def test_wrong_runner_architecture_is_not_accepted(self):
        self.prepare()
        result, run = self.run_mocked(expected_arch="x86_64")
        self.assertFalse(result["passed"])
        self.assertIn("host", result["error"])
        run.assert_not_called()

    def test_corrupt_artifact_is_not_executed(self):
        self.prepare()
        (self.bundle / "c-smoke.com").write_bytes(b"changed")
        result, run = self.run_mocked()
        self.assertFalse(result["passed"])
        self.assertIn("hash", result["error"])
        run.assert_not_called()

    def test_nonzero_exit_cannot_be_hidden_by_success_output(self):
        self.prepare()
        def run(command, **kwargs):
            self.mock_run(command, **kwargs)
            return subprocess.CompletedProcess(command, 1)
        result, run = self.run_mocked(side_effect=run)
        self.assertFalse(result["passed"])
        self.assertEqual(run.call_count, 6)
        self.assertFalse(any(p["passed"] for p in result["programs"]))

    def test_missing_output_marker_fails(self):
        self.prepare()
        result, _ = self.run_mocked(side_effect=lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
        self.assertFalse(result["passed"])

    def test_timeout_is_recorded_and_other_programs_still_run(self):
        self.prepare()
        def run(command, **kwargs):
            if Path(command[-1]).stem == "c-smoke":
                raise subprocess.TimeoutExpired(command, 180)
            return self.mock_run(command, **kwargs)
        result, run = self.run_mocked(side_effect=run)
        self.assertFalse(result["passed"])
        self.assertEqual(run.call_count, 6)
        self.assertEqual(sum(p["passed"] for p in result["programs"]), 5)

    def test_executable_mutation_is_a_failure(self):
        self.prepare()
        def run(command, **kwargs):
            completed = self.mock_run(command, **kwargs)
            Path(command[-1]).write_bytes(b"mutated")
            return completed
        result, _ = self.run_mocked(side_effect=run)
        self.assertFalse(result["passed"])
        self.assertFalse(any(p["passed"] for p in result["programs"]))


if __name__ == "__main__":
    unittest.main()
