"""Build-recipe tests only; these do not execute or assess macOS artifacts."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from link_rust_programs import link_programs


class LinkRustProgramsTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.sdk = self.root / "sdk"
        self.target = self.root / "target"
        self.output = self.root / "output"
        self.bin = self.sdk / "vendor/cosmocc/bin"
        self.bin.mkdir(parents=True)
        for name in ("apelink", "pecheck", "ape-x86_64.elf", "ape-aarch64.elf", "ape-m1.c"):
            (self.bin / name).write_bytes(b"build input")
        (self.sdk / "LICENSE").write_text("reference license")
        for arch in ("x86_64", "aarch64"):
            folder = self.target / f"{arch}-unknown-linux-musl/release"
            folder.mkdir(parents=True)
            (folder / "sample.com.dbg").write_bytes(b"compiled input " + arch.encode())

    def run_link(self, command, **kwargs):
        if command[1].endswith("/apelink"):
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"MZqFpD='linked fixture\n")
            output.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    def test_link_uses_both_compiled_inputs_and_all_requested_bootstrap_inputs(self):
        with patch("link_rust_programs.subprocess.run", side_effect=self.run_link) as run:
            link_programs(self.sdk, self.target, [("sample", "sample.com")], self.output, "0" * 40)
        command = run.call_args_list[0].args[0]
        self.assertIn(str(self.bin / "ape-x86_64.elf"), command)
        self.assertIn(str(self.bin / "ape-aarch64.elf"), command)
        self.assertEqual(command[command.index("-M") + 1], str(self.bin / "ape-m1.c"))
        self.assertIn(str(self.target / "x86_64-unknown-linux-musl/release/sample.com.dbg"), command)
        self.assertIn(str(self.target / "aarch64-unknown-linux-musl/release/sample.com.dbg"), command)
        manifest = json.loads((self.output / "BUILD.json").read_text())
        self.assertEqual(manifest["source_commit"], "0" * 40)
        self.assertEqual(manifest["architectures"], ["x86_64", "aarch64"])
        self.assertFalse(manifest["runtime_tested"])
        self.assertEqual(manifest["runtime_results"], "Published separately by execution jobs")
        self.assertNotIn("macos", manifest)
        self.assertEqual((self.output / "LICENSE.rust-ape").read_text(), "reference license")

    def test_missing_required_input_fails_before_link(self):
        (self.bin / "ape-m1.c").unlink()
        with patch("link_rust_programs.subprocess.run") as run:
            with self.assertRaises(FileNotFoundError):
                link_programs(self.sdk, self.target, [("sample", "sample.com")], self.output, "0" * 40)
        run.assert_not_called()

    def test_failed_link_is_not_retried_with_a_reduced_recipe(self):
        with patch("link_rust_programs.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, "apelink")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                link_programs(self.sdk, self.target, [("sample", "sample.com")], self.output, "0" * 40)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.output / "BUILD.json").exists())
        self.assertFalse((self.output / "sample.com").exists())

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing output")
        with self.assertRaises(FileExistsError):
            link_programs(self.sdk, self.target, [("sample", "sample.com")], self.output, "0" * 40)
        self.assertEqual(marker.read_text(), "existing output")

    def test_metadata_names_cannot_be_used_as_program_outputs(self):
        with self.assertRaisesRegex(ValueError, "metadata"):
            link_programs(self.sdk, self.target, [("sample", "BUILD.json")], self.output, "0" * 40)

    def test_failed_pecheck_does_not_publish_program(self):
        def run(command, **kwargs):
            if command[1].endswith("/pecheck"):
                raise subprocess.CalledProcessError(1, command)
            return self.run_link(command, **kwargs)
        with patch("link_rust_programs.subprocess.run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                link_programs(self.sdk, self.target, [("sample", "sample.com")], self.output, "0" * 40)
        self.assertFalse((self.output / "sample.com").exists())
        self.assertFalse((self.output / "BUILD.json").exists())


if __name__ == "__main__":
    unittest.main()
