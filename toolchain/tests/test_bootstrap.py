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
from bootstrap import RUST_NOTICES, bootstrap_machin, bootstrap_sdk, load_pins, stage_rust
from provenance import describe_tree
from file_metadata import tree_sha256


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

    def rust_sysroot(self):
        source = self.root / "rust-source"
        (source / "bin").mkdir(parents=True)
        (source / "lib/rustlib").mkdir(parents=True)
        for name in ("rustc", "cargo"):
            (source / "bin" / name).write_text("compiler fixture")
        (source / "lib/rustlib/multirust-channel-manifest.toml").write_text('date = "2026-07-28"\n')
        for name in RUST_NOTICES:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("notice " + name)
        return source

    def test_rust_staging_preserves_all_required_notices(self):
        source = self.rust_sysroot()
        destination = self.root / "rust-staged"
        with patch("bootstrap.capture", return_value="pinned compiler version"):
            stage_rust(source, destination, load_pins(self.root))
        for name in RUST_NOTICES:
            self.assertEqual((destination / name).read_bytes(), (source / name).read_bytes())
        stamp = json.loads((destination / ".toolchain-bootstrap.json").read_text())
        self.assertEqual(stamp["tree_sha256"], tree_sha256(destination))

    def test_missing_rust_notice_fails_before_staging(self):
        source = self.rust_sysroot()
        (source / RUST_NOTICES[0]).unlink()
        destination = self.root / "rust-staged"
        with self.assertRaises(FileNotFoundError):
            stage_rust(source, destination, load_pins(self.root))
        self.assertFalse(destination.exists())

    def test_wrong_rust_distribution_date_is_rejected(self):
        source = self.rust_sysroot()
        (source / "lib/rustlib/multirust-channel-manifest.toml").write_text('date = "2026-07-29"\n')
        with self.assertRaises(ValueError):
            stage_rust(source, self.root / "rust-staged", load_pins(self.root))

    def test_rust_staging_refuses_existing_destination(self):
        source = self.rust_sysroot()
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "valuable").write_text("keep")
        with self.assertRaises(FileExistsError):
            stage_rust(source, destination, load_pins(self.root))
        self.assertEqual((destination / "valuable").read_text(), "keep")


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
