"""Compiler archive preflight fixtures: no network, compiler or model execution."""

import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from source_runtime import (COMPILER_ARCHIVES, NETWORK_ENVIRONMENT, build_environment,
                           download_compiler_archive, load_compiler_archives,
                           prepare_compiler_archives)

PINNED = {
    "aarch64-gcc.zip": "6a07f915ec0296cd33b3142e75c00ed1a7072c75d92c82a0c0b5f5df2cff0dd2",
    "x86_64-gcc.zip": "cbb1659c56a0a4f95a71f59f94693515000d3dd53f79a597acacd53cbad2c7d8",
    "llvm.zip": "d42c2e46204d4332975d2d7464c5df63c898c34f8d9d2b83c168c14705ca8edd",
}
URL = "https://github.com/ahgamut/superconfigure/releases/download/z0.0.60/"


class CompilerArchiveCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.cache = self.root / "cache"
        self.pin_file = self.source / COMPILER_ARCHIVES
        self.pin_file.parent.mkdir(parents=True)
        self.data = {name: b"fixture archive bytes for " + name.encode() for name in PINNED}
        self.pin_file.write_text("".join(f"{name} {hashlib.sha256(data).hexdigest()} {URL}{name}\n"
                                         for name, data in self.data.items()))

    def populate(self):
        self.cache.mkdir()
        for name, data in self.data.items():
            (self.cache / name).write_bytes(data)

    def download(self, url, destination, env):
        destination.write_bytes(self.data[url.rsplit("/", 1)[-1]])

    def test_real_pins_are_exactly_the_original_three_release_assets(self):
        pins = load_compiler_archives(ROOT)
        self.assertEqual(pins, {name: {"sha256": checksum, "url": URL + name}
                                for name, checksum in PINNED.items()})
        shell = (ROOT / "tool/cosmocc/package.sh").read_text()
        self.assertIn("tool/cosmocc/compiler-archives.tsv", shell)
        for checksum in PINNED.values():
            self.assertNotIn(checksum, shell)

    def test_pins_reject_duplicates_missing_unknown_and_unofficial_rows(self):
        original = self.pin_file.read_text()
        for data in (original + original.splitlines()[0] + "\n",
                     "\n".join(original.splitlines()[:-1]),
                     original.replace("llvm.zip", "other.zip"),
                     original.replace("https://github.com/", "http://github.com/")):
            with self.subTest(data=data):
                self.pin_file.write_text(data)
                with self.assertRaises(ValueError):
                    load_compiler_archives(self.source)

    def test_both_pin_readers_accept_final_row_without_newline(self):
        expected = load_compiler_archives(self.source)
        self.pin_file.write_text(self.pin_file.read_text().rstrip("\n"))
        self.populate()
        self.assertEqual(load_compiler_archives(self.source), expected)
        result = self.run_shell(suffix=":\n")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse((self.root / "make").exists())
        self.assertFalse((self.root / "network").exists())

    def test_both_pin_readers_restrict_release_tag_characters(self):
        original = self.pin_file.read_text()
        self.pin_file.write_text(original.replace("z0.0.60", "Az09_.-"))
        self.assertEqual(set(load_compiler_archives(self.source)), set(PINNED))
        result = self.run_shell(cache=False, suffix=":\n")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for tag in ("", "z0.0.60/extra", "z?x", "z%x", "z@x", "z+x", "z:x", "zé"):
            with self.subTest(tag=tag):
                self.pin_file.write_text(original.replace("z0.0.60", tag))
                with self.assertRaises(ValueError):
                    load_compiler_archives(self.source)
                result = self.run_shell(cache=False, suffix=":\n")
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "make").exists())
        self.assertFalse((self.root / "network").exists())

    def test_allowlist_preserves_existing_proxy_ca_without_build_overrides(self):
        settings = {name: "fixture-" + name for name in NETWORK_ENVIRONMENT}
        settings.update(HOME="fixture-home", PATH="fixture-path", SYSTEMROOT="fixture-system",
                        MAKEFLAGS="bad", CC="bad", CXX="bad", CPATH="bad", LIBRARY_PATH="bad",
                        COSMOCC="bad", COSMO_RUNTIME_PROFILE="bad", GIT_CONFIG_COUNT="bad")
        with patch.dict(os.environ, settings, clear=True):
            actual = build_environment()
        self.assertEqual(set(actual), set(NETWORK_ENVIRONMENT) | {"HOME", "PATH", "SYSTEMROOT"})
        for name in actual:
            self.assertEqual(actual[name], settings[name])
        with patch.dict(os.environ, {"HTTPS_PROXY": "fixture-upper", "NO_PROXY": "fixture-no"}, clear=True):
            actual = build_environment()
        self.assertEqual(actual["https_proxy"], "fixture-upper")
        self.assertEqual(actual["no_proxy"], "fixture-no")

    def test_reuses_verified_flat_cache_without_network_or_modification(self):
        self.populate()
        before = {name: (self.cache / name).stat().st_ino for name in self.data}
        with patch("source_runtime.download_compiler_archive") as download:
            result, pins = prepare_compiler_archives(self.source, self.cache, {}, allow_download=False)
            download.assert_not_called()
        self.assertEqual(result, self.cache)
        self.assertEqual(pins, load_compiler_archives(self.source))
        self.assertEqual(before, {name: (self.cache / name).stat().st_ino for name in self.data})

    def test_explicit_cache_missing_never_creates_or_downloads(self):
        with patch("source_runtime.download_compiler_archive") as download:
            with self.assertRaisesRegex(ValueError, "must already exist"):
                prepare_compiler_archives(self.source, self.cache, {}, allow_download=False)
            self.assertFalse(self.cache.exists())
            self.cache.mkdir()
            (self.cache / "llvm.zip").write_bytes(self.data["llvm.zip"])
            with self.assertRaisesRegex(ValueError, "checksum/type mismatch"):
                prepare_compiler_archives(self.source, self.cache, {}, allow_download=False)
            download.assert_not_called()

    def test_default_cache_populates_verifies_and_reuses(self):
        with patch("source_runtime.download_compiler_archive", side_effect=self.download) as download:
            result, _ = prepare_compiler_archives(self.source, self.cache, {"https_proxy": "fixture"})
            self.assertEqual(download.call_count, 3)
            prepare_compiler_archives(self.source, self.cache, {})
            self.assertEqual(download.call_count, 3)
        self.assertEqual(result, self.cache)
        self.assertEqual(set(path.name for path in self.cache.iterdir()), set(self.data))
        for name, data in self.data.items():
            self.assertEqual((self.cache / name).read_bytes(), data)

    def test_bad_existing_or_symlink_archive_is_never_replaced_or_redownloaded(self):
        self.populate()
        target = self.cache / "x86_64-gcc.zip"
        target.write_bytes(b"corrupt cache")
        with patch("source_runtime.download_compiler_archive") as download:
            with self.assertRaisesRegex(ValueError, "checksum/type mismatch"):
                prepare_compiler_archives(self.source, self.cache, {})
            self.assertEqual(target.read_bytes(), b"corrupt cache")
            target.unlink()
            target.symlink_to("aarch64-gcc.zip")
            with self.assertRaisesRegex(ValueError, "checksum/type mismatch"):
                prepare_compiler_archives(self.source, self.cache, {})
            self.assertTrue(target.is_symlink())
            download.assert_not_called()

    def test_partial_failure_keeps_verified_downloads_but_no_partial_entry(self):
        def download(url, destination, env):
            self.download(url, destination, env)
            if destination.name == "x86_64-gcc.zip":
                raise ValueError("fixture network failure")
        with patch("source_runtime.download_compiler_archive", side_effect=download):
            with self.assertRaisesRegex(ValueError, "network failure"):
                prepare_compiler_archives(self.source, self.cache, {})
        self.assertEqual(set(path.name for path in self.cache.iterdir()), {"aarch64-gcc.zip"})
        with patch("source_runtime.download_compiler_archive", side_effect=self.download) as download:
            prepare_compiler_archives(self.source, self.cache, {})
            self.assertEqual(download.call_count, 2)

    def test_wrong_download_hash_is_not_published(self):
        def wrong(url, destination, env):
            destination.write_bytes(b"unverified bytes")
        with patch("source_runtime.download_compiler_archive", side_effect=wrong):
            with self.assertRaisesRegex(ValueError, "checksum/type mismatch"):
                prepare_compiler_archives(self.source, self.cache, {})
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_concurrent_wrong_entry_is_never_replaced(self):
        def race(downloaded, destination, **kwargs):
            destination.write_bytes(b"concurrent bytes")
            raise FileExistsError()
        with patch("source_runtime.download_compiler_archive", side_effect=self.download), \
                patch("source_runtime.os.link", side_effect=race):
            with self.assertRaisesRegex(ValueError, "checksum/type mismatch"):
                prepare_compiler_archives(self.source, self.cache, {})
        self.assertEqual((self.cache / "aarch64-gcc.zip").read_bytes(), b"concurrent bytes")

    def test_downloader_failure_does_not_disclose_proxy_or_ca_values(self):
        secret = "fixture-sensitive-proxy-and-ca"
        env = {"HTTPS_PROXY": secret, "CURL_CA_BUNDLE": secret}
        result = subprocess.CompletedProcess([], 1, stderr=secret.encode())
        with patch("source_runtime.shutil.which", return_value="curl"), \
                patch("source_runtime.subprocess.run", return_value=result) as run:
            with self.assertRaises(ValueError) as raised:
                download_compiler_archive(URL + "llvm.zip", self.root / "download", env)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs["env"]["HTTPS_PROXY"], secret)
        self.assertNotIn(secret, run.call_args.args[0])

    def test_wget_ca_settings_are_used_without_echoing_downloader_errors(self):
        env = {"SSL_CERT_FILE": "fixture-ca-file", "SSL_CERT_DIR": "fixture-ca-dir"}
        with patch("source_runtime.shutil.which", side_effect=[None, "wget"]), \
                patch("source_runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            download_compiler_archive(URL + "llvm.zip", self.root / "download", env)
        command = run.call_args.args[0]
        self.assertIn("--https-only", command)
        self.assertEqual(command[command.index("--ca-certificate") + 1], "fixture-ca-file")
        self.assertEqual(command[command.index("--ca-directory") + 1], "fixture-ca-dir")

    def shell_environment(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        for name, marker in (("make", "make"), ("gmake", "make"), ("wget", "network"),
                             ("curl", "network"), ("python3", "python")):
            path = bin_dir / name
            path.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(self.root / marker)) + "\nexit 79\n")
            path.chmod(0o755)
        return {"PATH": str(bin_dir) + os.pathsep + os.defpath, "COSMO_PACKAGE_JOBS": "1"}

    def run_shell(self, *, cache=True, suffix=None):
        env = self.shell_environment()
        if cache:
            env["COSMO_PACKAGE_ARCHIVE_CACHE"] = str(self.cache)
        script = (ROOT / "tool/cosmocc/package.sh").read_text()
        if suffix is None:
            command = ["sh", str(ROOT / "tool/cosmocc/package.sh"), str(self.root / "sdk")]
        else:
            script = script[:script.index("if ! MAKE=")] + suffix
            command = ["sh", "-c", script]
        return subprocess.run(command, cwd=self.source, env=env, capture_output=True)

    def test_shell_cache_mismatch_fails_before_first_make_without_network(self):
        self.populate()
        (self.cache / "llvm.zip").write_bytes(b"wrong")
        result = self.run_shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "make").exists())
        self.assertFalse((self.root / "network").exists())
        self.assertFalse((self.root / "sdk").exists())

    def test_shell_valid_cache_and_default_mode_do_not_require_python(self):
        self.populate()
        for cache in (True, False):
            with self.subTest(cache=cache):
                result = self.run_shell(cache=cache)
                self.assertEqual(result.returncode, 79, result.stderr.decode())
                self.assertTrue((self.root / "make").exists())
                self.assertFalse((self.root / "network").exists())
                self.assertFalse((self.root / "python").exists())
                (self.root / "make").unlink()

    def test_shell_fetch_uses_verified_copy_and_never_falls_back_when_cache_disappears(self):
        self.populate()
        result = self.run_shell(suffix='fetch "$ARM64_COMPILER_URL" "$ARM64_COMPILER_SHA256"\n')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.source / "aarch64-gcc.zip").read_bytes(), self.data["aarch64-gcc.zip"])
        result = self.run_shell(suffix='rm "$COSMO_PACKAGE_ARCHIVE_CACHE/llvm.zip"\n'
                               'fetch "$LLVM_COMPILER_URL" "$LLVM_COMPILER_SHA256"\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "network").exists())


if __name__ == "__main__":
    unittest.main()
