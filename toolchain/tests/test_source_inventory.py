"""Deferred behavioral coverage. Run only when CPU tests are authorized."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_identity import source_inventory_sha256
from source_runtime import tracked_source_inventory


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
