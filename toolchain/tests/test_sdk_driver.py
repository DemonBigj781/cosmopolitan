"""Compile the real driver with fixture ZIP resources and a traced exec boundary."""

import os
from pathlib import Path
import platform
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
static const char *fixture_path(const char *path, char *buffer) {
  if (!strncmp(path,"/zip/",5)) {
    snprintf(buffer,4096,"%s/%s",getenv("TEST_RESOURCES"),path+5);
    return buffer;
  }
  return path;
}
static FILE *fixture_fopen(const char *path,const char *mode) {
  char buffer[4096];return fopen(fixture_path(path,buffer),mode);
}
static int fixture_access(const char *path,int mode) {
  char buffer[4096];return access(fixture_path(path,buffer),mode);
}
static int fixture_execvp(const char *file,char *const args[]) {
  if(getenv("TEST_REAL_SHELL"))return execv("/bin/sh",args);
  dprintf(1,"exec=%s\n",file);
  for(int i=0;args[i];i++)dprintf(1,"arg=%s\n",args[i]);
  _exit(0);
}
#define fopen fixture_fopen
#define access fixture_access
#define execvp fixture_execvp
#include "toolchain/embedded-sdk-driver.c"
#undef fopen
int extract_sdk_zip(const char *archive,const char *root,enum SdkComponent component) {
  char path[4096];
  snprintf(path,sizeof(path),"%s/bin",root);if(mkdir(path,0700))return -1;
  snprintf(path,sizeof(path),"%s/bin/cosmocc",root);
  FILE *file=fopen(path,"w");if(!file)return -1;
  fputs("#!/bin/sh\nprintf 'descriptor=%s pid=%s user=%s\\n' \"${_COSMO_FDS_V2-unset}\" \"${_COSMO_PID-unset}\" \"${KEEP_ME-unset}\"\n",file);
  fclose(file);return chmod(path,0700);
}
int main(int argc,char **argv) {
  int result=embedded_sdk_main(argc,argv);
  if(embedded_sdk_cleanup())return 99;
  return result;
}
'''


class SdkDriverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        directory = Path(cls.build.name)
        (directory / "cosmo.h").write_text(
            '#define __static_yoink(x)\n#include <string.h>\n#include <stdlib.h>\n'
            '#define IsLinux() (!strcmp(getenv("TEST_HOST_OS"),"Linux"))\n'
            '#define IsWindows() (!strcmp(getenv("TEST_HOST_OS"),"Windows"))\n'
            '#define IsXnu() (!strcmp(getenv("TEST_HOST_OS"),"Darwin"))\n')
        (directory / "driver.c").write_text(HARNESS)
        cls.executable = directory / "driver"
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
            "-O0", "-I", str(directory), "-I", str(ROOT), str(directory / "driver.c"),
            str(ROOT / "toolchain/tempdir.c"), "-o", str(cls.executable)], check=True)

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        (self.root / "rust-ape").mkdir()
        (self.root / "rust-ape/run.sh").write_text("source")
        (self.root / "rust-ape/EMBEDDED.txt").write_text("Rust-APE source snapshot;")
        (self.root / "rust-toolchain/bin").mkdir(parents=True)
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        self.native_machine = 183 if platform.machine() in ("aarch64", "arm64") else 62
        header[18] = self.native_machine
        self.rustc = self.root / "rust-toolchain/bin/rustc"
        self.rustc.write_bytes(header)
        self.rustc.chmod(0o755)

    def run_driver(self, *args, system="Linux", extra_env=None):
        return subprocess.run([str(self.executable), *args], text=True, capture_output=True,
                              env=dict(os.environ, TEST_HOST_OS=system,
                                       TEST_RESOURCES=str(self.root), TMPDIR=str(self.root), **(extra_env or {})))

    def test_compiler_script_is_explicitly_invoked_through_sh(self):
        result = self.run_driver("cc", "--version", system="Windows")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2], ["exec=sh.exe", "arg=sh.exe"])
        self.assertEqual(result.stdout.splitlines()[2], "arg=-c")
        self.assertTrue(result.stdout.splitlines()[4].endswith("/bin/cosmocc"))
        self.assertEqual(result.stdout.splitlines()[-1], "arg=--version")

    def test_unix_shell_name_does_not_gain_a_windows_suffix(self):
        result = self.run_driver("cc", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2], ["exec=sh", "arg=sh"])

    def test_windows_drive_arguments_keep_shell_compatible_separators(self):
        result = self.run_driver("cc", "/C/source/input.c", "-o", r"D:\build\out.com",
                                 r"-DMESSAGE=literal\backslash", system="Windows")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("arg=C:/source/input.c", lines)
        self.assertIn("arg=D:/build/out.com", lines)
        self.assertIn(r"arg=-DMESSAGE=literal\backslash", lines)

    def test_unix_drive_like_arguments_are_not_rewritten(self):
        result = self.run_driver("cc", "/C/source/input.c", r"D:\build\out.com")
        self.assertIn("arg=/C/source/input.c", result.stdout.splitlines())
        self.assertIn(r"arg=D:\build\out.com", result.stdout.splitlines())

    def test_windows_shell_does_not_leak_cosmo_fd_handoff_to_its_children(self):
        result = self.run_driver("cc", "--version", system="Windows", extra_env={
            "TEST_REAL_SHELL": "1", "_COSMO_FDS_V2": "stale-descriptors",
            "_COSMO_PID": "stale-pid", "KEEP_ME": "ordinary-user-value"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "descriptor=unset pid=unset user=ordinary-user-value\n")

    def test_unsupported_rust_host_fails_before_sdk_extraction(self):
        for system in ("Windows", "Darwin"):
            with self.subTest(system=system):
                result = self.run_driver("rust", "rustc", "--version", system=system)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsupported packaged Rust host:", result.stderr)
                self.assertNotIn("SDK materialized", result.stderr)
                self.assertNotIn("exec=", result.stdout)

    def test_other_linux_architecture_is_rejected_before_extraction(self):
        header = bytearray(self.rustc.read_bytes())
        header[18] = 62 if self.native_machine == 183 else 183
        self.rustc.write_bytes(header)
        result = self.run_driver("rust", "rustc", "--version")
        self.assertIn("Unsupported packaged Rust host:", result.stderr)
        self.assertNotIn("SDK materialized", result.stderr)

    def test_status_identifies_packaged_compiler_platform(self):
        result = self.run_driver("rust", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = "ARM64" if self.native_machine == 183 else "x86-64"
        self.assertIn("Rust compiler platform: Linux " + name, result.stdout)


if __name__ == "__main__":
    unittest.main()
