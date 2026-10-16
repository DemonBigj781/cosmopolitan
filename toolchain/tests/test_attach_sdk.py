"""Unit-test attachment orchestration; full builds exercise the real zipcopy."""

import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from attach_sdk import attach_sdk


class AttachSdkTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.executable = self.root / "base.com"
        self.sdk = self.root / "sdk.zip"
        self.output = self.root / "combined.com"
        self.executable.write_bytes(b"MZqFpD='fixture executable\n")
        self.executable.chmod(0o755)
        with zipfile.ZipFile(self.executable, "a") as z:
            z.writestr(".symtab.amd64", b"original symbols")
        with zipfile.ZipFile(self.sdk, "w", compression=zipfile.ZIP_DEFLATED) as z:
            info = zipfile.ZipInfo("bin/cosmocc")
            info.external_attr = 0o100755 << 16
            z.writestr(info, b"compiler resource")
            z.writestr("rust-ape/EMBEDDED.txt", b"Rust-APE source snapshot;")

    def native_copy_contract(self, command, **kwargs):
        # Emulate the external tool contract, not its implementation or performance.
        with zipfile.ZipFile(command[-2]) as source:
            with Path(command[-1]).open("r+b") as stream:
                stream.seek(0, 2)
                with zipfile.ZipFile(stream, "w") as target:
                    for info in source.infolist():
                        target.writestr(copy.copy(info), source.read(info))
        return subprocess.CompletedProcess(command, 0)

    def attach(self):
        with patch("attach_sdk.subprocess.run", side_effect=self.native_copy_contract):
            attach_sdk(self.executable, self.sdk, self.output, Path("/sdk/bin/zipcopy"))

    def test_preserves_existing_resources_sdk_and_executable_prefix(self):
        executable_bytes = self.executable.read_bytes()
        sdk_bytes = self.sdk.read_bytes()
        self.attach()
        self.assertTrue(self.output.read_bytes().startswith(executable_bytes))
        self.assertEqual(self.executable.read_bytes(), executable_bytes)
        self.assertEqual(self.sdk.read_bytes(), sdk_bytes)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o755)
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(z.read(".symtab.amd64"), b"original symbols")
            self.assertEqual(z.read("bin/cosmocc"), b"compiler resource")
            self.assertEqual(len(z.namelist()), len(set(z.namelist())))
            self.assertTrue(all(i.header_offset >= len(executable_bytes)
                                for i in z.infolist()))
            self.assertEqual(z.getinfo("bin/cosmocc").external_attr >> 16, 0o100755)

    def test_resource_collision_preserves_previous_output(self):
        self.output.write_bytes(b"previous validated output")
        with zipfile.ZipFile(self.sdk, "a") as z:
            z.writestr(".symtab.amd64", b"conflicting symbols")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.attach()
        self.assertEqual(self.output.read_bytes(), b"previous validated output")

    def test_failed_zipcopy_preserves_previous_output(self):
        self.output.write_bytes(b"previous validated output")
        with patch("attach_sdk.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, "zipcopy")):
            with self.assertRaises(subprocess.CalledProcessError):
                attach_sdk(self.executable, self.sdk, self.output, Path("zipcopy"))
        self.assertEqual(self.output.read_bytes(), b"previous validated output")

    def test_executable_without_previous_resources_is_supported(self):
        self.executable.write_bytes(b"MZqFpD='no existing resources\n")
        self.attach()
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(set(z.namelist()), {"bin/cosmocc", "rust-ape/EMBEDDED.txt"})

    def test_incomplete_zipcopy_output_is_rejected(self):
        self.output.write_bytes(b"previous validated output")
        with patch("attach_sdk.subprocess.run", return_value=None):
            with self.assertRaisesRegex(ValueError, "inventory"):
                attach_sdk(self.executable, self.sdk, self.output, Path("zipcopy"))
        self.assertEqual(self.output.read_bytes(), b"previous validated output")

    def test_input_cannot_be_replaced(self):
        original = self.executable.read_bytes()
        with self.assertRaisesRegex(ValueError, "input"):
            attach_sdk(self.executable, self.sdk, self.executable, Path("zipcopy"))
        self.assertEqual(self.executable.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
