"""Exercise the actual packaging heredoc and native extractor with small SDKs."""

import copy
import json
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
sys.path.insert(0, str(ROOT / "toolchain"))
from bootstrap import RUST_NOTICES, RUST_HOSTS, stage_rust, load_pins
from file_metadata import sha256
from provenance import record_artifact
from rust_fixtures import make_sysroot, binary_fixture

HOST = RUST_HOSTS[0]
RUST_PREFIX = "rust-toolchain/hosts/" + HOST + "/sysroot"


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
        sources = {host: make_sysroot(self.workdir / "sources", host) for host in RUST_HOSTS}
        stage_rust(sources, self.compiler, load_pins(ROOT))
        (self.compiler / ".toolchain-bootstrap.json").unlink()
        self.write(self.sdk, "generated/x86_64-unknown-linux-musl.json", "{}")
        for arch in ("x86_64", "aarch64"):
            name = f"{arch}-unknown-linux-musl.json"
            template = ROOT / "third_party/rust_ape/upstream/targets" / name
            self.write(self.sdk, "targets/" + name, template.read_text())
        self.write(self.sdk, "scripts/gcc-linker-wrapper.bash", "#!/bin/sh\nexit 0\n", executable=True)
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

    def run_packager(self, with_rust=True, machin=None):
        args = [str(self.cosmocc)]
        args += ([str(self.source), str(self.compiler), str(self.sdk)]
                 if with_rust else ["", "", ""])
        args.append(str(ROOT / "toolchain"))
        args.extend([str(machin) if machin else "", str(machin / "compiler") if machin else ""])
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
                         RUST_PREFIX + "/bin/rustc", RUST_PREFIX + "/bin/cargo",
                         RUST_PREFIX + "/lib/librustc_driver.so", "rust-ape/run.sh",
                         "rust-ape/EMBEDDED.txt", "rust-ape-sdk/LICENSE",
                         "rust-ape-sdk/generated/x86_64-unknown-linux-musl.json",
                         "rust-ape-sdk/vendor/cosmocc/bin/apelink",
                         "rust-ape-sdk/vendor/library/std/src/lib.rs",
                         "rust-ape-sdk/vendor/patches/libc/build.rs"):
                self.assertIn(name, archive.namelist())
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.getinfo("lib/libc.a").date_time[0], 1980)
            self.assertTrue(archive.getinfo("bin/cosmocc").external_attr >> 16 & 0o111)

    def test_packaged_rust_definitions_are_generated_without_host_paths(self):
        old = self.sdk / "generated/x86_64-unknown-linux-musl.json"
        old.write_text(json.dumps({"linker": str(self.sdk / "old-linker")}))
        original = old.read_bytes()
        with self.package() as archive:
            for arch in ("x86_64", "aarch64"):
                path = f"rust-ape-sdk/generated/{arch}-unknown-linux-musl.json"
                target = json.loads(archive.read(path))
                self.assertEqual(target["linker"], f"linker-{arch}.bash")
            for name in archive.namelist():
                if name.startswith("rust-ape-sdk/generated/"):
                    self.assertNotIn(str(self.sdk).encode(), archive.read(name), name)
            self.assertIn("rust-ape-sdk/generated/cargo.cosmo.toml", archive.namelist())
        self.assertEqual(old.read_bytes(), original)

    def test_selected_rust_component_rejects_cross_component_symlink(self):
        with self.package():
            pass
        with zipfile.ZipFile(self.archive, "a") as archive:
            info = zipfile.ZipInfo(RUST_PREFIX + "/bin/foreign-compiler")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../../../../bin/cosmocc")
        destination = self.workdir / "cross-component"
        destination.mkdir()
        result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "rust", HOST],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink rejected", result.stderr)

    def test_extraction_does_not_write_through_directory_symlink(self):
        outside = self.workdir / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("preserve")
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, target in (("a", "."), ("b", "a/../outside")):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, target)
            archive.writestr("b/keep.txt", "must not overwrite")
        destination = self.workdir / "symlink-parents"
        destination.mkdir()
        result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "c"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "preserve")

    def test_extraction_refuses_existing_leaf_symlinks(self):
        marker = self.workdir / "valuable.txt"
        marker.write_text("preserve")
        destination = self.workdir / "leaf-link"
        destination.mkdir()
        (destination / "alias").symlink_to(marker)
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("alias", "must not overwrite")
        result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "c"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "preserve")

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
                             {"bin/cosmocc", "include/stdio.h", "lib/libc.a", "toolchain-metadata/BUILD.json"})

    def test_notices_and_honest_provenance_are_packaged(self):
        with self.package() as archive:
            for host in RUST_HOSTS:
                for name in RUST_NOTICES:
                    self.assertEqual(archive.read("rust-toolchain/hosts/" + host + "/sysroot/" + name).decode(), "notice " + name)
            record = json.loads(archive.read("toolchain-metadata/BUILD.json"))
            self.assertEqual(record["inputs"]["cosmocc"]["verification"], "unverified-local-input")
            self.assertEqual(record["inputs"]["rust_compiler"]["verification"], "unverified-local-input")
            self.assertEqual(len(record["notices"]), len(RUST_NOTICES) * len(RUST_HOSTS))
            self.assertNotIn(str(self.workdir), json.dumps(record))

    def test_missing_compiler_notice_fails_before_packaging(self):
        (self.compiler / "hosts" / HOST / "sysroot" / RUST_NOTICES[0]).unlink()
        result = self.run_packager()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.archive.exists())

    def test_sidecar_records_actual_artifact_and_validates_notice_hashes(self):
        with self.package():
            pass
        record_artifact(self.archive)
        record = json.loads((self.archive.parent / "BUILD.json").read_text())
        self.assertEqual(record["artifact"]["sha256"], sha256(self.archive))
        self.assertEqual(record["artifact"]["size"], self.archive.stat().st_size)
        self.assertEqual((self.archive.parent / "SHA256SUMS").read_text(),
                         f"{sha256(self.archive)}  {self.archive.name}\n")

    def test_sidecar_rejects_altered_notice(self):
        with self.package() as archive:
            entries = [(info, archive.read(info)) for info in archive.infolist()]
        with zipfile.ZipFile(self.archive, "w") as archive:
            for info, data in entries:
                archive.writestr(info, b"changed" if info.filename == RUST_PREFIX + "/" + RUST_NOTICES[0] else data)
        with self.assertRaisesRegex(ValueError, "notice differs"):
            record_artifact(self.archive)
        self.assertFalse((self.archive.parent / "BUILD.json").exists())

    def test_machin_license_is_packaged_without_a_rust_payload(self):
        machin = self.workdir / "machin"
        self.write(machin, "LICENSE", "Machin license fixture")
        self.write(machin, "compiler", "compiler fixture")
        result = self.run_packager(with_rust=False, machin=machin)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(archive.read("toolchain-metadata/licenses/machin/LICENSE"), b"Machin license fixture")
            record = json.loads(archive.read("toolchain-metadata/BUILD.json"))
            self.assertIn("machin", record["inputs"])

    def test_c_extraction_does_not_materialize_rust_trees(self):
        with self.package():
            pass
        destination = self.workdir / "c-only"
        destination.mkdir()
        result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "c"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((destination / "bin/cosmocc").read_text(), "compiler")
        for name in ("rust-ape", "rust-toolchain", "rust-ape-sdk"):
            self.assertFalse((destination / name).exists(), name)

    def test_rust_extraction_does_not_materialize_c_or_patched_sdk(self):
        with self.package():
            pass
        destination = self.workdir / "rust-only"
        destination.mkdir()
        result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "rust", HOST],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((destination / RUST_PREFIX / "bin/rustc").read_bytes(), binary_fixture(HOST))
        for name in ("bin", "include", "rust-ape", "rust-ape-sdk"):
            self.assertFalse((destination / name).exists(), name)

    def test_components_can_be_materialized_once_into_same_directory(self):
        with self.package():
            pass
        destination = self.workdir / "combined"
        destination.mkdir()
        for component in ("rust", "c"):
            result = subprocess.run([str(self.extractor), str(self.archive), str(destination), component] +
                                    ([HOST] if component == "rust" else []),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((destination / "bin/cosmocc").is_file())
        self.assertTrue((destination / RUST_PREFIX / "bin/rustc").is_file())
        self.assertFalse((destination / "rust-ape-sdk").exists())

    def test_rust_extraction_selects_one_host_and_keeps_shared_target_aliases(self):
        with self.package() as archive:
            names = archive.namelist()
            target = "rust-toolchain/targets/" + HOST + "/lib/libcore.rlib"
            alias = RUST_PREFIX + "/lib/rustlib/" + HOST + "/lib"
            self.assertLess(names.index(target), names.index(alias))
            self.assertTrue(archive.read(alias).endswith(b"/"))
        for host in RUST_HOSTS:
            with self.subTest(host=host):
                destination = self.workdir / host
                destination.mkdir()
                result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "rust", host],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                root = destination / "rust-toolchain"
                self.assertEqual([p.name for p in (root / "hosts").iterdir()], [host])
                alias = root / "hosts" / host / "sysroot/lib/rustlib" / HOST / "lib"
                self.assertTrue(alias.is_symlink())
                self.assertEqual((alias / "libcore.rlib").read_text(), "shared target " + HOST)
                self.assertEqual((root / "hosts" / host / "sysroot/lib/rustlib" / host / "bin/rust-lld").read_text(), "native linker " + host)
                for other in set(RUST_HOSTS) - {host}:
                    self.assertFalse((root / "hosts" / other).exists())

    def test_patched_sdk_export_preserves_only_its_namespace(self):
        with self.package():
            pass
        destination = self.workdir / "ape-only"
        destination.mkdir()
        result = subprocess.run([str(self.extractor), str(self.archive), str(destination), "ape"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in destination.iterdir()}, {"rust-ape-sdk"})
        link = destination / "rust-ape-sdk/vendor/cosmocc/bin/aarch64-linux-cosmo-as"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "assembler")

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
