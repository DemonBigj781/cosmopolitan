"""Owned temporary-directory cleanup must not follow links or delete replacements."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TempDirTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="tempdir-tests-")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "tempdir-test"
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) +
                       ["-O0", "-I", str(ROOT),
                        str(ROOT / "toolchain/tests/tempdir_test.c"),
                        str(ROOT / "toolchain/tempdir.c"), "-o", str(cls.binary)], check=True)

    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "valuable.txt"
            sentinel.write_text("must survive")
            env = dict(os.environ, TMPDIR=str(root))
            result = subprocess.run([str(self.binary), mode, str(outside)],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr +
                             repr([str(p.relative_to(root)) for p in root.rglob("*")]))
            owned = Path(result.stdout.strip())
            self.assertEqual(owned.parent, root.resolve())
            self.assertEqual(sentinel.read_text(), "must survive")
            if mode == "replace":
                self.assertTrue((owned / "keep").is_file())
                self.assertTrue((Path(str(owned) + "-moved") / "nested/data").is_file())
            else:
                self.assertFalse(owned.exists())

    def test_cleanup_removes_owned_files_but_not_link_targets(self):
        self.run_case("normal")

    def test_cleanup_refuses_a_replacement_root(self):
        self.run_case("replace")


if __name__ == "__main__":
    unittest.main()
