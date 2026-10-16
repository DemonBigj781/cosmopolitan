"""Exercise the actual packaging heredoc and native extractor with small SDKs."""

import copy
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "toolchain/package-compiler-sdk.sh"


class SdkPackagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory(prefix="sdk-extractor-test-")
        cls.addClassCleanup(cls.build_dir.cleanup)
        cls.extractor = Path(cls.build_dir.name) / "extract-sdk"
        # One compiler invocation, no parallel build workers.
        subprocess.run(
            shlex.split(os.environ.get("CC", "cc"))
            + ["-O0", "-Werror=multichar", "-I", str(ROOT),
               str(ROOT / "toolchain/tests/extract_sdk_test.c"),
               "-lz", "-o", str(cls.extractor)],
            check=True,
        )
        script = SCRIPT.read_text()
        start = script.index(" <<'PY'\n") + len(" <<'PY'\n")
        cls.packager = script[start:script.index("\nPY\n", start)]

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="sdk-packaging-test-")
        self.addCleanup(self.work.cleanup)
        self.workdir = Path(self.work.name)
        self.cosmocc = self.workdir / "cosmocc"
        self.source = self.workdir / "source"
        self.compiler = self.workdir / "compiler"
        self.sdk = self.workdir / "sdk"
        self.archive = self.workdir / "o/toolchain/compiler-payload/cosmocc-sdk.zip"
        self.archive.parent.mkdir(parents=True)
        self.write(self.cosmocc, "bin/cosmocc", "compiler", executable=True)
        self.write(self.cosmocc, "include/stdio.h", "header")
        old = self.write(self.cosmocc, "lib/libc.a", "library")
        os.utime(old, (0, 0))
        self.write(self.source, "run.sh", "source snapshot", executable=True)
        self.write(self.compiler, "bin/rustc", "rust compiler", executable=True)
        self.write(self.compiler, "bin/cargo", "cargo", executable=True)
        self.write(self.compiler, "lib/librustc_driver.so", "compiler library")
        self.write(self.sdk, "generated/x86_64-unknown-linux-musl.json", "{}")
        self.write(self.sdk, "vendor/cosmocc/bin/apelink", "linker", executable=True)
        self.write(self.sdk, "vendor/cosmocc/libexec/gcc/aarch64-linux-cosmo/14.1.0/as",
                   "assembler", executable=True)
        (self.sdk / "vendor/cosmocc/bin/aarch64-linux-cosmo-as").symlink_to(
            "../libexec/gcc/aarch64-linux-cosmo/14.1.0/as"
        )
        self.write(self.sdk, "vendor/library/std/src/lib.rs", "standard library")
        self.write(self.sdk, "vendor/patches/libc/build.rs", "build script")
        self.write(self.sdk, "vendor/patches/example/build/mod.rs", "source module")
        self.write(self.sdk, "vendor/patches/example/target/mod.rs", "target module")
        self.write(self.sdk, "vendor/patches/example/cache/mod.rs", "cache module")
        self.write(self.sdk, "LICENSE", "license")
        for directory in (".git", "__pycache__", "target", "build", "o", "cache"):
            self.write(self.sdk, directory + "/unneeded", "not a runtime resource")

    def write(self, root, name, content, executable=False):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if executable:
            path.chmod(0o755)
        return path

    def run_packager(self, with_rust=True):
        args = [str(self.cosmocc)]
        args += ([str(self.source), str(self.compiler), str(self.sdk)]
                 if with_rust else ["", "", ""])
        return subprocess.run(
            [sys.executable, "-c", self.packager, *args],
            cwd=self.workdir, text=True, capture_output=True,
        )

    def package(self, with_rust=True):
        result = self.run_packager(with_rust)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Duplicate name", result.stderr)
        return zipfile.ZipFile(self.archive)

    def test_archive_paths_are_unique(self):
        with self.package() as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)), "SDK entries packaged twice")

    def test_excludes_only_disposable_sdk_contents(self):
        with self.package() as archive:
            names = set(archive.namelist())
            for directory in (".git", "__pycache__", "target", "build", "o", "cache"):
                with self.subTest(directory=directory):
                    self.assertNotIn("rust-ape-sdk/" + directory + "/unneeded", names)
            self.assertIn("rust-ape-sdk/vendor/patches/example/build/mod.rs", names)
            self.assertIn("rust-ape-sdk/vendor/patches/example/target/mod.rs", names)
            self.assertIn("rust-ape-sdk/vendor/patches/example/cache/mod.rs", names)

    def test_preserves_required_resources_and_metadata(self):
        with self.package() as archive:
            for name in ("bin/cosmocc", "include/stdio.h", "lib/libc.a",
                         "rust-toolchain/bin/rustc", "rust-toolchain/bin/cargo",
                         "rust-toolchain/lib/librustc_driver.so", "rust-ape/run.sh",
                         "rust-ape/EMBEDDED.txt", "rust-ape-sdk/LICENSE",
                         "rust-ape-sdk/generated/x86_64-unknown-linux-musl.json",
                         "rust-ape-sdk/vendor/cosmocc/bin/apelink",
                         "rust-ape-sdk/vendor/library/std/src/lib.rs",
                         "rust-ape-sdk/vendor/patches/libc/build.rs"):
                self.assertIn(name, archive.namelist())
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.getinfo("lib/libc.a").date_time[0], 1980)
            self.assertTrue(archive.getinfo("bin/cosmocc").external_attr >> 16 & 0o111)

    def test_native_extractor_accepts_packaged_sdk_symlink(self):
        with self.package():
            pass
        destination = self.workdir / "extracted"
        destination.mkdir()
        result = subprocess.run(
            [str(self.extractor), str(self.archive), str(destination)],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        link = destination / "rust-ape-sdk/vendor/cosmocc/bin/aarch64-linux-cosmo-as"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "assembler")
        self.assertTrue(os.access(link, os.X_OK))

    def test_c_only_payload_remains_supported(self):
        with self.package(with_rust=False) as archive:
            self.assertEqual(set(archive.namelist()),
                             {"bin/cosmocc", "include/stdio.h", "lib/libc.a"})

    def test_native_extractor_reads_absolute_offsets_inside_executable(self):
        executable = self.workdir / "embedded.com"
        executable.write_bytes(b"MZqFpD='executable prefix\n")
        with self.package() as source, zipfile.ZipFile(executable, "a") as target:
            for info in source.infolist():
                target.writestr(copy.copy(info), source.read(info))
        destination = self.workdir / "embedded-extracted"
        destination.mkdir()
        result = subprocess.run(
            [str(self.extractor), str(executable), str(destination)],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((destination / "bin/cosmocc").read_text(), "compiler")
        link = destination / "rust-ape-sdk/vendor/cosmocc/bin/aarch64-linux-cosmo-as"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "assembler")

    def test_colliding_input_paths_fail_validation(self):
        self.write(self.cosmocc, "rust-ape-sdk/LICENSE", "conflicting SDK entry")
        result = self.run_packager()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SDK archive contains duplicate paths", result.stderr)

    def test_missing_patched_target_fails_before_packaging(self):
        (self.sdk / "generated/x86_64-unknown-linux-musl.json").unlink()
        result = self.run_packager()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUST_APE_SDK_ROOT must contain", result.stderr)
        self.assertFalse(self.archive.exists())

    def test_native_extractor_still_rejects_unsafe_symlinks(self):
        for target in ("../outside", "/absolute", "..\\outside"):
            with self.subTest(target=target):
                with zipfile.ZipFile(self.archive, "w") as archive:
                    info = zipfile.ZipInfo("escape")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, target)
                destination = self.workdir / "unsafe-extracted"
                destination.mkdir(exist_ok=True)
                result = subprocess.run(
                    [str(self.extractor), str(self.archive), str(destination)],
                    text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlink rejected", result.stderr)
                self.assertFalse((destination / "escape").is_symlink())


if __name__ == "__main__":
    unittest.main()
