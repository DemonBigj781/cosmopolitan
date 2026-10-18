"""Compile the real driver with fixture ZIP resources and a traced exec boundary."""

import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from rust_fixtures import binary_fixture
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
  if(getenv("TEST_FLOOD")) {
    char out[4096],err[4096];memset(out,'A',sizeof(out));memset(err,'B',sizeof(err));
    for(int i=0;i<128;i++) {
      if(write(1,out,sizeof(out))!=sizeof(out)||write(2,err,sizeof(err))!=sizeof(err))_exit(76);
    }
    _exit(0);
  }
  if(getenv("TEST_EXPECT_PIPES")) {
    struct stat out,err;
    if(fstat(1,&out)||fstat(2,&err)||!S_ISFIFO(out.st_mode)||!S_ISFIFO(err.st_mode))_exit(77);
  }
  if(getenv("TEST_REAL_SHELL"))return execv("/bin/sh",args);
  dprintf(1,"exec=%s\n",file);
  for(int i=0;args[i];i++)dprintf(1,"arg=%s\n",args[i]);
  for(int i=0;args[i];i++)if(strstr(args[i],"/cargo")) {
    if(getenv("RUSTC"))dprintf(1,"rustc=%s\n",getenv("RUSTC"));
    if(getenv("RUSTDOC"))dprintf(1,"rustdoc=%s\n",getenv("RUSTDOC"));
  }
  _exit(0);
}
#define fopen fixture_fopen
#define access fixture_access
#define execvp fixture_execvp
#include "toolchain/embedded-sdk-driver.c"
#undef fopen
int extract_sdk_zip(const char *archive,const char *root,enum SdkComponent component,const char *host) {
  char path[4096];
  if(component==SDK_RUST) {
    const char *pieces[]={"rust-toolchain","hosts",host,"sysroot","bin"};
    snprintf(path,sizeof(path),"%s",root);
    for(int i=0;i<5;i++){strcat(path,"/");strcat(path,pieces[i]);if(mkdir(path,0700))return -1;}
    for(int i=0;i<2;i++) {
      char source[4096],target[4096];
      const char *name=i?"cargo":"rustc",*suffix=IsWindows()?".exe":"";
      snprintf(source,sizeof(source),"%s/rust-toolchain/hosts/%s/sysroot/bin/%s%s",getenv("TEST_RESOURCES"),host,name,suffix);
      snprintf(target,sizeof(target),"%s/%s%s",path,name,suffix);
      FILE *in=fopen(source,"rb"),*out=fopen(target,"wb");if(!in||!out)return -1;
      char buffer[512];size_t size;
      while((size=fread(buffer,1,sizeof(buffer),in)))fwrite(buffer,1,size,out);
      fclose(in);fclose(out);if(chmod(target,0700))return -1;
    }
    return 0;
  }
  snprintf(path,sizeof(path),"%s/bin",root);if(mkdir(path,0700))return -1;
  snprintf(path,sizeof(path),"%s/bin/cosmocc",root);
  FILE *file=fopen(path,"w");if(!file)return -1;
  fputs("#!/bin/sh\nprintf 'descriptor=%s pid=%s user=%s limit=%s\\n' \"${_COSMO_FDS_V2-unset}\" \"${_COSMO_PID-unset}\" \"${KEEP_ME-unset}\" \"${_COSMO_RLIMIT-unset}\"\n",file);
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
        self.native_machine = 183 if platform.machine() in ("aarch64", "arm64") else 62
        self.host = ("aarch64" if self.native_machine == 183 else "x86_64") + "-unknown-linux-gnu"
        self.rustc = self.add_host(self.host)

    def add_host(self, host):
        directory = self.root / "rust-toolchain/hosts" / host / "sysroot/bin"
        directory.mkdir(parents=True)
        suffix = ".exe" if "windows" in host else ""
        for name in ("rustc", "cargo"):
            path = directory / (name + suffix)
            path.write_bytes(binary_fixture(host))
            path.chmod(0o755)
        return directory / ("rustc" + suffix)

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
            "_COSMO_PID": "stale-pid", "KEEP_ME": "ordinary-user-value",
            "_COSMO_RLIMIT": "resource-policy"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "descriptor=unset pid=unset user=ordinary-user-value limit=resource-policy\n")

    def test_windows_shell_receives_pipes_instead_of_caller_log_handles(self):
        with (self.root / "stdout.log").open("w") as stdout, (self.root / "stderr.log").open("w") as stderr:
            result = subprocess.run([str(self.executable), "cc", "--version"], stdout=stdout, stderr=stderr,
                env=dict(os.environ, TEST_HOST_OS="Windows", TEST_RESOURCES=str(self.root),
                         TMPDIR=str(self.root), TEST_EXPECT_PIPES="1"))
        self.assertEqual(result.returncode, 0)
        self.assertIn("exec=sh.exe", (self.root / "stdout.log").read_text())
        self.assertIn("SDK materialized: C", (self.root / "stderr.log").read_text())

    def test_windows_relay_drains_both_streams_beyond_pipe_capacity(self):
        result = subprocess.run([str(self.executable), "cc", "--version"], text=True,
            capture_output=True, timeout=20, env=dict(os.environ, TEST_HOST_OS="Windows",
                TEST_RESOURCES=str(self.root), TMPDIR=str(self.root), TEST_FLOOD="1"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "A" * (4096 * 128))
        self.assertEqual(result.stderr.split("\n", 1)[1], "B" * (4096 * 128))

    def test_missing_host_payload_fails_before_sdk_extraction(self):
        for system in ("Windows", "Darwin"):
            with self.subTest(system=system):
                result = self.run_driver("rust", "rustc", "--version", system=system)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Rust compiler not packaged for", result.stderr)
                self.assertNotIn("SDK materialized", result.stderr)
                self.assertNotIn("exec=", result.stdout)

    def test_other_linux_architecture_is_rejected_before_extraction(self):
        header = bytearray(self.rustc.read_bytes())
        header[18] = 62 if self.native_machine == 183 else 183
        self.rustc.write_bytes(header)
        result = self.run_driver("rust", "rustc", "--version")
        self.assertIn("Packaged Rust compiler does not match", result.stderr)
        self.assertNotIn("SDK materialized", result.stderr)

    def test_status_identifies_packaged_compiler_platform(self):
        result = self.run_driver("rust", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Rust compiler platform: " + self.host, result.stdout)

    def test_native_rust_uses_its_host_sysroot(self):
        result = self.run_driver("rust", "rustc", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/hosts/" + self.host + "/sysroot/bin/rustc", result.stdout)
        self.assertIn("arg=--sysroot", result.stdout)

    def test_macos_rust_payload_is_selected_for_current_process_architecture(self):
        host = ("aarch64" if self.native_machine == 183 else "x86_64") + "-apple-darwin"
        self.add_host(host)
        result = self.run_driver("rust", "rustc", "--version", system="Darwin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/hosts/" + host + "/sysroot/bin/rustc", result.stdout)

    def test_windows_rust_and_cargo_use_exe_files_and_clear_handoff_before_native_exec(self):
        host = "x86_64-pc-windows-msvc"
        self.add_host(host)
        for command in ("rustc", "cargo"):
            result = self.run_driver("rust", command, "--version", system="Windows")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("/hosts/" + host + "/sysroot/bin/" + command + ".exe", result.stdout)
            self.assertIn('"$0" "$@"; result=$?; exit "$result"', result.stdout)
            self.assertNotIn('exec "$0" "$@"', result.stdout)
            if command == "cargo":
                self.assertIn("rustc=", result.stdout)
                self.assertIn("/bin/rustc.exe", result.stdout)
                self.assertIn("/bin/rustdoc.exe", result.stdout)


if __name__ == "__main__":
    unittest.main()
