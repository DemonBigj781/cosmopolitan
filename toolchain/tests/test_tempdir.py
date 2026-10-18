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


class WindowsImageReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="image-release-tests-")
        cls.addClassCleanup(cls.build.cleanup)
        root = Path(cls.build.name)
        source = root / "release.c"
        source.write_text(r'''
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
static int attempts[2],failures;
static int fixture_unlinkat(int fd,const char *name,int flags) {
  int index=(!strcmp(name,"held-a.exe")||!strcmp(name,"held.txt"))?0:!strcmp(name,"held-b.dll")?1:-1;
  if(index>=0 && ++attempts[index]<=failures){errno=EACCES;return -1;}
  return unlinkat(fd,name,flags);
}
static int fixture_sleep(const struct timespec *delay,struct timespec *remaining){return 0;}
#define _WIN32 1
#define unlinkat fixture_unlinkat
#define nanosleep fixture_sleep
#include "toolchain/tempdir.c"
int main(int argc,char **argv) {
  int nonimage=atoi(argv[1])==-1;
  failures=nonimage?1000:atoi(argv[1]);
  struct ToolchainTemp dir=TOOLCHAIN_TEMP_INIT;
  if(toolchain_temp_open(&dir,"release-test"))return 2;
  const char *names[]={nonimage?"held.txt":"held-a.exe","held-b.dll"};
  for(int i=0;i<(failures==70?2:1);i++) {
    int fd=openat(dir.fd,names[i],O_CREAT|O_WRONLY,0600);
    if(fd<0||write(fd,"keep",4)!=4||close(fd))return 3;
  }
  int rc=toolchain_temp_close(&dir),error=errno,total=attempts[0]+attempts[1];
  printf("%d %d %d\n",rc,error,total);
  if(nonimage)return rc==-1 && error==EACCES && total==1 ? 0 : 6;
  if(failures==3)return rc==0 && total==4 ? 0 : 4;
  return rc==-1 && error==EACCES && total==(failures==70?102:101) ? 0 : 5;
}
''')
        cls.binary = root / "release-test"
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) +
                       ["-O0", "-I", str(ROOT), str(source), "-o", str(cls.binary)], check=True)

    def run_case(self, failures, remaining):
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run([str(self.binary), str(failures)], env=dict(os.environ, TMPDIR=root),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            files = list(Path(root).glob("release-test-*/held*"))
            self.assertEqual(len(files), remaining)
            for file in files:
                self.assertEqual(file.read_bytes(), b"keep")

    def test_transient_image_lock_is_released(self):
        self.run_case(3, 0)

    def test_permanent_permission_denial_is_not_hidden(self):
        self.run_case(1000, 1)

    def test_wait_budget_is_shared_across_the_owned_tree(self):
        self.run_case(70, 1)

    def test_non_image_permission_denial_is_not_retried(self):
        self.run_case(-1, 1)


if __name__ == "__main__":
    unittest.main()
