"""Bootstrap orchestration tests use fixtures, not network downloads or builds."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from bootstrap import RUST_NOTICES, RUST_HOSTS, ELF_TARGETS, bootstrap_machin, bootstrap_sdk, bootstrap_rust, load_pins, stage_rust, rust_hosts
from provenance import describe_tree
from file_metadata import tree_sha256
from rust_fixtures import make_sysroot


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        (self.root / "toolchain").mkdir()
        self.lock = self.root / "toolchain/bootstrap.lock.json"
        self.lock.write_bytes((ROOT / "toolchain/bootstrap.lock.json").read_bytes())
        self.upstream = self.root / "third_party/rust_ape/upstream"
        (self.upstream / "xtask/src").mkdir(parents=True)
        (self.upstream / "xtask/src/config.rs").write_text(
            'pub const COSMOCC_VERSION: &str = "4.0.2";\n'
            'pub const COSMOCC_SHA256: &str =\n    "' + 'a' * 64 + '";\n')
        (self.upstream / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "nightly-2026-07-28"\n')

    def test_pins_reuse_vendored_sdk_definitions(self):
        pins = load_pins(self.root)
        self.assertEqual(pins["cosmocc"]["version"], "4.0.2")
        self.assertEqual(pins["cosmocc"]["sha256"], 'a' * 64)
        self.assertEqual(pins["rust_nightly"], "nightly-2026-07-28")
        self.assertEqual(pins["go_version"], "1.23.12")

    def test_mutable_source_revision_is_rejected(self):
        value = json.loads(self.lock.read_text())
        value["machin"]["revision"] = "main"
        self.lock.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            load_pins(self.root)

    def test_floating_minor_go_version_is_rejected(self):
        value = json.loads(self.lock.read_text())
        value["go_version"] = "1.23"
        self.lock.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            load_pins(self.root)

    def test_duplicate_sdk_pin_declaration_is_rejected(self):
        path = self.upstream / "xtask/src/config.rs"
        path.write_text(path.read_text() + 'pub const COSMOCC_VERSION: &str = "4.0.2";\n')
        with self.assertRaises(ValueError):
            load_pins(self.root)

    def test_existing_sdk_is_not_silently_trusted_or_overwritten(self):
        destination = self.root / "sdk"
        destination.mkdir()
        marker = destination / "keep"
        marker.write_text("existing SDK")
        with patch("bootstrap.subprocess.run") as run:
            with self.assertRaises(FileExistsError):
                bootstrap_sdk(destination, load_pins(self.root), self.root)
        run.assert_not_called()
        self.assertEqual(marker.read_text(), "existing SDK")

    def test_sdk_download_receives_version_and_checksum(self):
        destination = self.root / "sdk"
        def download(command, **kwargs):
            staging = Path(command[2])
            (staging / "bin").mkdir(parents=True)
            (staging / "bin/cosmocc").write_text("compiler")
            (staging / "bin/zipcopy").write_text("zip tool")
            (staging.parent / "current").symlink_to(staging.name)
            return subprocess.CompletedProcess(command, 0)
        pins = load_pins(self.root)
        with patch("bootstrap.subprocess.run", side_effect=download) as run:
            bootstrap_sdk(destination, pins, self.root)
        self.assertEqual(run.call_args.args[0][-2:], ["4.0.2", 'a' * 64])
        stamp = json.loads((destination / ".toolchain-bootstrap.json").read_text())
        self.assertEqual(stamp["tree_sha256"], tree_sha256(destination))
        self.assertEqual(stamp["cosmocc"], pins["cosmocc"])
        self.assertFalse((destination.parent / "current").exists())
        record = describe_tree(destination)
        self.assertEqual(record["bootstrap"]["verification"], "archive-sha256-checked")
        (destination / "bin/cosmocc").write_text("modified")
        with self.assertRaisesRegex(ValueError, "changed"):
            describe_tree(destination)

    def test_failed_download_does_not_publish_partial_sdk(self):
        destination = self.root / "sdk"
        def download(command, **kwargs):
            staging = Path(command[2])
            staging.mkdir()
            (staging / "partial").write_text("interrupted")
            raise subprocess.CalledProcessError(1, command)
        with patch("bootstrap.subprocess.run", side_effect=download):
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap_sdk(destination, load_pins(self.root), self.root)
        self.assertFalse(destination.exists())

    def test_unstamped_local_input_is_not_claimed_as_verified(self):
        self.assertEqual(describe_tree(self.upstream)["verification"], "unverified-local-input")

    def test_machin_bootstrap_fetches_exact_revision_and_records_binary(self):
        source, binary = self.root / "machin", self.root / "machin-compiler"
        pins = load_pins(self.root)
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[:2] == ["go", "build"]:
                Path(command[command.index("-o") + 1]).write_text("compiler")
                (Path(kwargs["cwd"]) / "LICENSE").write_text("license")
            return subprocess.CompletedProcess(command, 0)
        def capture(command, **kwargs):
            if command[0] == "go":
                return "go version go1.23.12 linux/amd64"
            return pins["machin"]["revision"]
        with patch("bootstrap.subprocess.run", side_effect=run), patch("bootstrap.capture", side_effect=capture):
            bootstrap_machin(source, binary, pins)
        self.assertTrue(any(c[-3:] == ["--depth=1", "origin", pins["machin"]["revision"]] for c in commands))
        self.assertTrue(binary.is_file())
        self.assertEqual(describe_tree(source)["bootstrap"]["machin"], pins["machin"])

    def test_wrong_go_version_fails_before_fetch(self):
        with patch("bootstrap.capture", return_value="go version go1.24.0 linux/amd64"), patch("bootstrap.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "Go version"):
                bootstrap_machin(self.root / "machin", self.root / "compiler", load_pins(self.root))
        run.assert_not_called()

    def rust_sysroot(self, host="x86_64-unknown-linux-gnu"):
        return make_sysroot(self.root, host)

    def test_rust_staging_preserves_all_required_notices(self):
        source = self.rust_sysroot()
        destination = self.root / "rust-staged"
        stage_rust({RUST_HOSTS[0]: source}, destination, load_pins(self.root))
        for name in RUST_NOTICES:
            self.assertEqual((destination / "hosts" / RUST_HOSTS[0] / "sysroot" / name).read_bytes(), (source / name).read_bytes())
        stamp = json.loads((destination / ".toolchain-bootstrap.json").read_text())
        self.assertEqual(stamp["tree_sha256"], tree_sha256(destination))

    def test_missing_rust_notice_fails_before_staging(self):
        source = self.rust_sysroot()
        (source / RUST_NOTICES[0]).unlink()
        destination = self.root / "rust-staged"
        with self.assertRaises(FileNotFoundError):
            stage_rust({RUST_HOSTS[0]: source}, destination, load_pins(self.root))
        self.assertFalse(destination.exists())

    def test_wrong_rust_distribution_date_is_rejected(self):
        source = self.rust_sysroot()
        (source / "lib/rustlib/multirust-channel-manifest.toml").write_text('date = "2026-07-29"\n')
        with self.assertRaises(ValueError):
            stage_rust({RUST_HOSTS[0]: source}, self.root / "rust-staged", load_pins(self.root))

    def test_rust_staging_refuses_existing_destination(self):
        source = self.rust_sysroot()
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "valuable").write_text("keep")
        with self.assertRaises(FileExistsError):
            stage_rust({RUST_HOSTS[0]: source}, destination, load_pins(self.root))
        self.assertEqual((destination / "valuable").read_text(), "keep")

    def test_multiple_hosts_share_identical_target_libraries_without_losing_dlls(self):
        sources = {host: self.rust_sysroot(host) for host in RUST_HOSTS}
        destination = self.root / "multi-host"
        with patch("bootstrap.capture") as capture:
            stage_rust(sources, destination, load_pins(self.root))
        capture.assert_not_called()  # Foreign-host compilers must not be executed.
        for host in RUST_HOSTS:
            for target in set((*ELF_TARGETS, host)):
                alias = destination / "hosts" / host / "sysroot/lib/rustlib" / target / "lib"
                self.assertTrue(alias.is_symlink())
                self.assertEqual(alias.resolve(), destination / "targets" / target / "lib")
                self.assertEqual((alias / "libcore.rlib").read_text(), "shared target " + target)
            tools = destination / "hosts" / host / "sysroot/lib/rustlib" / host / "bin"
            self.assertTrue(tools.is_dir())
            self.assertEqual((tools / "rust-lld").read_text(), "native linker " + host)
        windows = destination / "hosts/x86_64-pc-windows-msvc/sysroot/bin"
        self.assertTrue((windows / "rustc.exe").is_file())
        self.assertEqual((windows / "rustc_driver-fixture.dll").read_bytes(), b"native DLL")
        self.assertEqual(json.loads((destination / "hosts.json").read_text())["hosts"], list(RUST_HOSTS))

    def test_conflicting_target_libraries_fail_instead_of_overwriting(self):
        first, second = RUST_HOSTS[:2]
        sources = {host: self.rust_sysroot(host) for host in (first, second)}
        (sources[second] / "lib/rustlib" / ELF_TARGETS[0] / "lib/libcore.rlib").write_text("different")
        destination = self.root / "conflict"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            stage_rust(sources, destination, load_pins(self.root))
        self.assertFalse(destination.exists())

    def test_mislabeled_native_compiler_is_rejected(self):
        source = self.rust_sysroot()
        with self.assertRaisesRegex(ValueError, "compiler host"):
            stage_rust({"aarch64-unknown-linux-gnu": source}, self.root / "wrong-host", load_pins(self.root))

    def test_rust_bootstrap_installs_exact_foreign_hosts_in_private_storage(self):
        pins = load_pins(self.root)
        destination = self.root / "downloaded-rust"
        def install(command, **kwargs):
            self.assertEqual(command[:3], ["/usr/bin/rustup", "toolchain", "install"])
            self.assertIn("--force-non-host", command)
            self.assertIn("--no-self-update", command)
            host = command[3].removeprefix(pins["rust_nightly"] + "-")
            self.assertIn(host, RUST_HOSTS)
            home = Path(kwargs["env"]["RUSTUP_HOME"])
            self.assertTrue(home.is_relative_to(self.root))
            source = make_sysroot(home / "toolchains", host)
            source.rename(home / "toolchains" / command[3])
            return subprocess.CompletedProcess(command, 0)
        with patch("bootstrap.shutil.which", return_value="/usr/bin/rustup"), \
             patch("bootstrap.subprocess.run", side_effect=install) as run:
            bootstrap_rust(destination, pins)
        self.assertEqual(run.call_count, len(RUST_HOSTS))
        self.assertEqual(rust_hosts(destination), list(RUST_HOSTS))


class FileMetadataTest(unittest.TestCase):
    def test_tree_hash_detects_file_and_link_changes_without_following_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            sdk.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "data").write_text("outside")
            (sdk / "link").symlink_to(outside, target_is_directory=True)
            (sdk / "file").write_text("one")
            first = tree_sha256(sdk)
            (outside / "data").write_text("changed outside")
            self.assertEqual(first, tree_sha256(sdk))
            (sdk / "file").write_text("two")
            self.assertNotEqual(first, tree_sha256(sdk))
            second = tree_sha256(sdk)
            (sdk / "file").chmod(0o755)
            self.assertNotEqual(second, tree_sha256(sdk))
            third = tree_sha256(sdk)
            (sdk / "link").unlink()
            (sdk / "link").symlink_to("other-target")
            self.assertNotEqual(third, tree_sha256(sdk))


if __name__ == "__main__":
    unittest.main()
