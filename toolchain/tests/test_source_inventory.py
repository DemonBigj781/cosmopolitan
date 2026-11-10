"""Deferred behavioral coverage. Run only when CPU tests are authorized."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_metadata import STAMP, tree_sha256
from runtime_identity import (SOURCE_INPUTS, read_json, required_files,
                              source_inventory_sha256, verify_runtime_sdk)
from source_runtime import (build_runtime_sdk, git, git_bytes, materialize_source,
                           publish_new_directory, tracked_source_inventory)


class SourceInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Source inventory fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.filemode", "true")
        self.git("remote", "add", "origin", "https://example.invalid/owner/source.git")
        (self.root / ".gitignore").write_bytes(b"/o/\n/.cosmocc/\n")
        (self.root / "input.txt").write_bytes(b"source\x00bytes\n")
        (self.root / "recipe.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.root / "recipe.sh").chmod(0o755)
        (self.root / "link").symlink_to("input.txt")
        (self.root / "nested").mkdir()
        (self.root / "nested/other.txt").write_bytes(b"other tracked input\n")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "source inventory fixture")
        self.revision = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null",
                               "-c", "commit.gpgsign=false", *args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def snapshot(self):
        return tracked_source_inventory(self.root, self.revision, self.tree)

    def new_output(self, name="source"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / name

    def crlf_checkout(self, with_sdk_sources=False):
        (self.root / ".gitattributes").write_text("*.bat text eol=crlf filter=tripwire\n"
                                                "input.txt export-ignore\nnested/other.txt export-subst\n")
        (self.root / "build.bat").write_bytes(b"@echo off\necho source\n")
        (self.root / "nested/other.txt").write_bytes(b"$Format:%H$\n")
        if with_sdk_sources:
            source_bytes = {
                "libc/nt/struct/sid.h": b"struct NtSidIdentifierAuthority {\n};\n",
                "libc/dlopen/dladdr.c": b"void dladdr(void) {}\n",
                "libc/intrin/fds.h": b"atomic_char kind;\nbool was_created_during_vfork;\nunsigned flags;\nlong handle;\n",
                "libc/proc/posix_spawn.c": b"void posix_spawn(void) {}\n",
            }
            for name, data in source_bytes.items():
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            pin = self.root / "third_party/rust_ape/source-runtime.json"
            pin.parent.mkdir(parents=True, exist_ok=True)
            pin.write_text(json.dumps({"runtime_abi": "cosmopolitan-source-runtime-v1",
                                      "source_abi_inputs": {name: hashlib.sha256(data).hexdigest()
                                                            for name, data in source_bytes.items()}}))
            recipe = self.root / "tool/cosmocc/package.sh"
            recipe.parent.mkdir(parents=True, exist_ok=True)
            recipe.write_bytes(b"#!/bin/sh\nexit 99 # fixture package is mocked\n")
            recipe.chmod(0o755)
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "CRLF checkout fixture")
        self.revision = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        ordinary = self.new_output("ordinary")
        git_bytes(self.root, "clone", "--quiet", "--no-local", "--", str(self.root), str(ordinary))
        git(ordinary, "config", "remote.origin.url", "https://example.invalid/owner/source.git")
        self.assertIn(b"\r\n", (ordinary / "build.bat").read_bytes())
        self.assertEqual(git(ordinary, "status", "--porcelain"), "")
        return ordinary

    def test_raw_materialization_preserves_committed_bytes_origin_and_caller(self):
        ordinary = self.crlf_checkout()
        before = (ordinary / "build.bat").read_bytes()
        with self.assertRaisesRegex(ValueError, "differ from the pinned Git blob"):
            tracked_source_inventory(ordinary, self.revision, self.tree)
        output = self.new_output()
        record = materialize_source(ordinary, output, self.revision)
        self.assertEqual((ordinary / "build.bat").read_bytes(), before)
        self.assertEqual((output / "build.bat").read_bytes(), b"@echo off\necho source\n")
        self.assertEqual((output / "input.txt").read_bytes(), b"source\x00bytes\n")
        self.assertEqual((output / "nested/other.txt").read_bytes(), b"$Format:%H$\n")
        self.assertEqual(git(output, "remote", "get-url", "origin"),
                         "https://example.invalid/owner/source.git")
        files = tracked_source_inventory(output, self.revision, self.tree)
        self.assertEqual(record["source_tree_sha256"], source_inventory_sha256(files))
        self.assertEqual(files["link"]["target"], "input.txt")
        self.assertTrue((output / "link").is_symlink())
        self.assertEqual(files["recipe.sh"]["mode"], "100755")
        self.assertEqual(git_bytes(output, "ls-files", "--others", "--exclude-standard", "-z"), b"")

    def test_materialization_never_executes_clean_smudge_or_inherited_config(self):
        ordinary = self.crlf_checkout()
        marker = self.new_output("filter-executed")
        script = marker.parent / "filter.py"
        script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('called')\n"
                          "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        command = shlex.join([sys.executable, str(script), str(marker)])
        for field in ("clean", "smudge", "process"):
            git(ordinary, "config", "filter.tripwire." + field, command)
        git(ordinary, "config", "filter.tripwire.required", "true")
        home = self.new_output("home")
        home.mkdir()
        (home / ".gitconfig").write_text("[filter \"tripwire\"]\n"
                                         f" clean = {command}\n smudge = {command}\n required = true\n")
        output = self.new_output()
        with patch.dict(os.environ, {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
                                     "GIT_WORK_TREE": str(ordinary), "GIT_INDEX_FILE": str(home / "wrong-index"),
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.tripwire.process",
                                     "GIT_CONFIG_VALUE_0": command}):
            materialize_source(ordinary, output)
            tracked_source_inventory(output, self.revision, self.tree)
        self.assertFalse(marker.exists())
        self.assertIn(b"\r\n", (ordinary / "build.bat").read_bytes())

    def test_materialization_selects_committed_content_without_copying_local_edits(self):
        (self.root / "input.txt").write_bytes(b"uncommitted content")
        (self.root / "untracked.txt").write_bytes(b"not committed")
        self.git("add", "input.txt")
        output = self.new_output()
        materialize_source(self.root, output)
        self.assertEqual((output / "input.txt").read_bytes(), b"source\x00bytes\n")
        self.assertFalse((output / "untracked.txt").exists())
        self.assertEqual((self.root / "input.txt").read_bytes(), b"uncommitted content")

    def test_materialization_refuses_existing_or_internal_output_and_short_revision(self):
        output = self.new_output()
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "pre-existing output"):
            materialize_source(self.root, output)
        with self.assertRaisesRegex(ValueError, "outside the input repository"):
            materialize_source(self.root, self.root / "raw-source")
        with self.assertRaisesRegex(ValueError, "full committed revision"):
            materialize_source(self.root, self.new_output(), self.revision[:8])

    def test_materialization_rejects_committed_submodule(self):
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.revision},submodule")
        self.git("commit", "--quiet", "-m", "committed submodule fixture")
        output = self.new_output()
        with self.assertRaisesRegex(ValueError, "Submodules require explicit"):
            materialize_source(self.root, output)
        self.assertFalse(output.exists())

    def test_materialization_publication_cannot_replace_concurrent_empty_directory(self):
        output = self.new_output()

        def race(source, destination):
            destination.mkdir()
            publish_new_directory(source, destination)

        with patch("source_runtime.publish_new_directory", side_effect=race):
            with self.assertRaises(FileExistsError):
                materialize_source(self.root, output)
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])

    def test_sdk_build_uses_same_raw_checkout_bytes_and_records_them(self):
        ordinary = self.crlf_checkout(with_sdk_sources=True)
        raw = self.new_output()
        materialize_source(ordinary, raw)
        expected = tracked_source_inventory(raw, self.revision, self.tree)
        bootstrap = self.new_output("bootstrap")
        bootstrap.mkdir()
        (bootstrap / "compiler").write_bytes(b"bootstrap fixture")
        pin = {"version": "1.2.3", "sha256": "a" * 64}
        (bootstrap / STAMP).write_text(json.dumps({
            "schema_version": 1, "cosmocc": pin, "verification": "archive-sha256-checked",
            "tree_sha256": tree_sha256(bootstrap)}))
        output = self.new_output("sdk")
        original_run = subprocess.run
        packages = []

        def run(command, *args, **kwargs):
            if command[:2] == ["sh", "tool/cosmocc/package.sh"]:
                checkout = kwargs["cwd"]
                self.assertEqual(tracked_source_inventory(checkout, self.revision, self.tree), expected)
                self.assertEqual((checkout / "build.bat").read_bytes(), b"@echo off\necho source\n")
                staged = Path(command[2])
                for name in required_files():
                    path = staged / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"SDK fixture, not compiled output")
                packages.append(checkout)
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, *args, **kwargs)

        with patch("source_runtime.load_pins", return_value={"cosmocc": pin}), \
                patch("source_runtime.subprocess.run", side_effect=run):
            record = build_runtime_sdk(raw, bootstrap, output)
        self.assertEqual(len(packages), 1)
        self.assertEqual(read_json(output / SOURCE_INPUTS)["files"], expected)
        self.assertEqual(verify_runtime_sdk(output), record)
        self.assertEqual(record["source_tree_sha256"], source_inventory_sha256(expected))
        self.assertEqual(tracked_source_inventory(raw, self.revision, self.tree), expected)
        self.assertIn(b"\r\n", (ordinary / "build.bat").read_bytes())

    def test_sdk_build_rejects_internal_output_before_git_hashing_or_writes(self):
        output = self.root / "o/new-sdk"
        before = self.snapshot()
        with patch("source_runtime.git") as inspect_git, \
                patch("source_runtime.tracked_source_inventory") as hash_source, \
                patch("source_runtime.inventory") as hash_bootstrap, \
                patch("source_runtime.materialize_source") as materialize, \
                patch("source_runtime.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "SDK output must be outside the source checkout"):
                build_runtime_sdk(self.root, self.root / "missing-bootstrap", output)
            for operation in (inspect_git, hash_source, hash_bootstrap, materialize, run):
                operation.assert_not_called()
        self.assertFalse(output.parent.exists())
        self.assertFalse(output.exists())
        self.assertEqual(self.snapshot(), before)

    def test_complete_source_file_bytes_modes_and_link_target_bytes(self):
        files = self.snapshot()
        self.assertEqual(set(files), {".gitignore", "input.txt", "recipe.sh", "link", "nested/other.txt"})
        self.assertEqual(files["input.txt"], {
            "mode": "100644", "type": "file", "size": 13,
            "sha256": hashlib.sha256(b"source\x00bytes\n").hexdigest(),
        })
        self.assertEqual(files["recipe.sh"]["mode"], "100755")
        self.assertEqual(files["link"], {
            "mode": "120000", "type": "symlink", "size": 9, "target": "input.txt",
            "sha256": hashlib.sha256(b"input.txt").hexdigest(),
        })
        self.assertNotEqual(source_inventory_sha256(files), hashlib.sha256(self.tree.encode()).hexdigest())

    def test_generated_outputs_are_not_source_inputs(self):
        before = self.snapshot()
        (self.root / "o").mkdir()
        (self.root / "o/generated.o").write_bytes(b"host build output")
        (self.root / ".cosmocc").mkdir()
        (self.root / ".cosmocc/compiler").write_bytes(b"bootstrap input")
        (self.root / "untracked.log").write_text("build output")
        self.assertEqual(self.snapshot(), before)

    def test_assume_unchanged_cannot_hide_tracked_byte_drift(self):
        self.git("update-index", "--assume-unchanged", "input.txt")
        (self.root / "input.txt").write_bytes(b"edited hidden bytes")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "differ from the pinned Git blob"):
            self.snapshot()

    def test_staged_changes_are_not_the_pinned_tree(self):
        (self.root / "input.txt").write_bytes(b"different source")
        self.git("add", "input.txt")
        with self.assertRaisesRegex(ValueError, "index differs from the pinned"):
            self.snapshot()

    def test_deleted_and_extra_index_entries_are_rejected(self):
        self.git("rm", "--cached", "input.txt")
        with self.assertRaisesRegex(ValueError, "index differs from the pinned"):
            self.snapshot()
        self.git("reset", "--quiet", "HEAD", "input.txt")
        (self.root / "new.txt").write_text("uncommitted addition")
        self.git("add", "new.txt")
        with self.assertRaisesRegex(ValueError, "index differs from the pinned"):
            self.snapshot()

    def test_changed_executable_mode_is_rejected(self):
        (self.root / "recipe.sh").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "changed type or mode"):
            self.snapshot()

    def test_source_symlink_is_never_followed(self):
        (self.root / "link").unlink()
        (self.root / "link").symlink_to("missing-source-target")
        self.git("add", "link")
        self.git("commit", "--quiet", "-m", "dangling source link fixture")
        self.revision = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        entry = self.snapshot()["link"]
        self.assertEqual(entry["sha256"], hashlib.sha256(b"missing-source-target").hexdigest())

    def test_changed_source_symlink_target_is_rejected(self):
        (self.root / "link").unlink()
        (self.root / "link").symlink_to("recipe.sh")
        with self.assertRaisesRegex(ValueError, "differ from the pinned Git blob"):
            self.snapshot()

    def test_every_tracked_file_changes_the_complete_digest(self):
        before = source_inventory_sha256(self.snapshot())
        (self.root / "nested/other.txt").write_bytes(b"changed non-ABI source\n")
        self.git("add", "nested/other.txt")
        self.git("commit", "--quiet", "-m", "change ordinary tracked input")
        self.revision = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.assertNotEqual(source_inventory_sha256(self.snapshot()), before)

    def test_file_replaced_with_link_to_same_bytes_is_rejected(self):
        path = self.root / "input.txt"
        path.rename(self.root / "copy.txt")
        path.symlink_to("copy.txt")
        with self.assertRaisesRegex(ValueError, "changed type or mode"):
            self.snapshot()

    def test_source_directory_link_is_rejected(self):
        (self.root / "nested").rename(self.root / "moved")
        (self.root / "nested").symlink_to("moved", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "non-directory ancestor"):
            self.snapshot()

    def test_submodule_is_not_treated_as_complete_source(self):
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.revision},submodule")
        with self.assertRaisesRegex(ValueError, "Submodules require explicit"):
            self.snapshot()

    def test_wrong_revision_or_tree_is_rejected(self):
        for revision, tree in (("0" * 40, self.tree), (self.revision, "0" * 40)):
            with self.subTest(revision=revision, tree=tree):
                with self.assertRaisesRegex(ValueError, "pinned revision/tree"):
                    tracked_source_inventory(self.root, revision, tree)


if __name__ == "__main__":
    unittest.main()
