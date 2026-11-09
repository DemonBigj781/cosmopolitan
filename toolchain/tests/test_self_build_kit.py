"""Deferred behavioral coverage. Run only when CPU tests are authorized."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
import package_self_build as packager


class SelfBuildKitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="self-build-kit-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.sdk = self.root / "sdk"
        self.sdk.mkdir()
        self.output = self.root / "kit.zip"
        profile = patch.dict(os.environ, {"COSMO_RUNTIME_PROFILE": "stock-sdk"})
        profile.start()
        self.addCleanup(profile.stop)
        for name in packager.SOURCE_NAMES:
            self.write("toolchain/" + name)
        self.source = self.write("o/toolchain/compiler.c", "stale compiler source")
        self.headers = {
            "common": self.write("toolchain/language_build.h"),
            "x86_64": self.write("libc/x86_only.h"),
            "aarch64": self.write("libc/arm_only.h"),
        }
        for name in (
            "build/bootstrap/tlscc", "build/bootstrap/fixupobj",
            "o/toolchain/lua_entry.o", "o/tool/net/ljson.o",
            "o/toolchain/.aarch64/lua_entry.o", "o/tool/net/.aarch64/ljson.o",
            "o/ape/ape-no-modify-self.o", "o/ape/ape.lds", "o/libc/crt/crt.o",
            "o/aarch64/ape/aarch64.lds", "o/aarch64/libc/crt/crt.o",
            "o/third_party/lua/lua.a", "o/aarch64/third_party/lua/lua.a",
        ):
            self.write(name)
        self.x86_object = self.root / "external-x86.o"
        self.arm_object = self.root / "external-arm.o"
        self.x86_object.write_bytes(b"prebuilt x86")
        self.arm_object.write_bytes(b"prebuilt arm")
        deps = patch.object(packager, "source_dependencies", side_effect=self.source_deps)
        self.dependencies = deps.start()
        self.addCleanup(deps.stop)
        lua = patch.object(packager, "run_lua_deps", side_effect=lambda repo, make, mode: [
            "o/" + (mode + "/" if mode else "") + "third_party/lua/lua.a",
        ])
        lua.start()
        self.addCleanup(lua.stop)

    def write(self, relative, content="fixture"):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def source_deps(self, repo, sdk, source, *, arch, machin):
        return [source, self.headers["common"], self.headers[arch]]

    def collect(self, **options):
        return packager.collect_entries(self.repo, self.sdk, "make", **options)

    def package(self, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            packager.write_kit(self.repo, self.sdk, self.output, "make", **options)
        with zipfile.ZipFile(self.output) as archive:
            return json.loads(archive.read(packager.MANIFEST_PATH))

    def test_absent_machin_ignores_stale_generated_source(self):
        entries, responses = self.collect()
        self.assertFalse(any(name.endswith("/compiler.c") for name in entries))
        self.assertFalse(any(name.endswith("/compiler.o") for name in entries))
        for response in responses.values():
            self.assertNotIn(b"compiler.o", response)
        self.assertEqual(self.package()["machin_mode"], "absent")

    def test_source_machin_is_recompiled_for_both_architectures(self):
        options = {"machin_source": self.source}
        entries, responses = self.collect(**options)
        self.assertIn("self-build/source/o/toolchain/compiler.c", entries)
        for arch in ("x86_64", "aarch64"):
            response = responses[f"self-build/link/{arch}.rsp"].decode().splitlines()
            self.assertIn(f"work/{arch}/compiler.o", response)
            self.assertNotIn(f"objects/{arch}/compiler.o", response)
        machin_calls = [call for call in self.dependencies.call_args_list
                        if call.args[2] == self.source]
        self.assertEqual({call.kwargs["arch"] for call in machin_calls}, {"x86_64", "aarch64"})
        self.assertTrue(all(call.kwargs["machin"] for call in machin_calls))
        self.assertEqual(self.package(**options)["machin_mode"], "source")

    def test_prebuilt_machin_preserves_objects_without_claiming_source_rebuild(self):
        options = {"machin_object": self.x86_object, "machin_arm_object": self.arm_object}
        entries, responses = self.collect(**options)
        self.assertNotIn("self-build/source/o/toolchain/compiler.c", entries)
        for arch, path in (("x86_64", self.x86_object), ("aarch64", self.arm_object)):
            self.assertEqual(entries[f"self-build/objects/{arch}/compiler.o"][0], path)
            response = responses[f"self-build/link/{arch}.rsp"].decode().splitlines()
            self.assertIn(f"objects/{arch}/compiler.o", response)
            self.assertNotIn(f"work/{arch}/compiler.o", response)
        self.assertEqual(self.package(**options)["machin_mode"], "prebuilt")
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("self-build/objects/x86_64/compiler.o"), b"prebuilt x86")
            self.assertEqual(archive.read("self-build/objects/aarch64/compiler.o"), b"prebuilt arm")

    def test_ambiguous_and_incomplete_machin_inputs_are_rejected_before_probing(self):
        for options in (
            {"machin_source": self.source, "machin_object": self.x86_object},
            {"machin_source": self.source, "machin_arm_object": self.arm_object},
            {"machin_source": self.source, "machin_object": self.x86_object,
             "machin_arm_object": self.arm_object},
            {"machin_object": self.x86_object},
            {"machin_arm_object": self.arm_object},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.collect(**options)
        self.dependencies.assert_not_called()

    def test_explicit_source_must_exist_at_the_native_builders_canonical_path(self):
        with self.assertRaises(ValueError):
            self.collect(machin_source=self.headers["common"])
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.collect(machin_source=self.source)

    def test_missing_prebuilt_object_is_not_silently_omitted(self):
        self.arm_object.unlink()
        with self.assertRaises(FileNotFoundError):
            self.collect(machin_object=self.x86_object, machin_arm_object=self.arm_object)

    def test_header_closure_unions_both_architectures_and_language_build(self):
        entries, _ = self.collect()
        for path in self.headers.values():
            self.assertIn("self-build/source/" + path.relative_to(self.repo).as_posix(), entries)
        for name in packager.SOURCE_NAMES:
            calls = [call for call in self.dependencies.call_args_list
                     if call.args[2].name == name]
            self.assertEqual({call.kwargs["arch"] for call in calls}, {"x86_64", "aarch64"})
            self.assertTrue(all(not call.kwargs["machin"] for call in calls))

    def test_archive_and_inventory_are_deterministic(self):
        manifest = self.package()
        first = self.output.read_bytes()
        self.assertEqual(manifest["runtime"], {"kind": "stock-sdk"})
        self.assertEqual(manifest["entry_count"], len(manifest["entries"]))
        self.assertEqual(self.package(), manifest)
        self.assertEqual(self.output.read_bytes(), first)

    def test_input_change_between_hashing_and_writing_never_publishes(self):
        target = self.repo / "toolchain/ape_toolchain.c"
        original_hash = packager.sha256_file

        def mutate_after_hash(path):
            digest = original_hash(path)
            if path == target:
                path.write_bytes(b"after!")  # Same length, different bytes.
            return digest

        for existing in (False, True):
            with self.subTest(existing=existing):
                target.write_bytes(b"before")
                if existing:
                    self.output.write_bytes(b"previous valid output")
                with patch.object(packager, "sha256_file", side_effect=mutate_after_hash):
                    with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                        self.package()
                if existing:
                    self.assertEqual(self.output.read_bytes(), b"previous valid output")
                else:
                    self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".kit.zip.*.tmp")))

    def test_archived_permissions_are_verified(self):
        original_info = packager.archive_info

        def wrong_permissions(name, mode):
            return original_info(name, mode ^ 0o100)

        with patch.object(packager, "archive_info", side_effect=wrong_permissions):
            with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                self.package()
        self.assertFalse(self.output.exists())

    def test_runtime_is_verified_before_collection(self):
        with patch.object(packager, "selected_runtime", side_effect=ValueError("mixed runtime")):
            with self.assertRaisesRegex(ValueError, "mixed runtime"):
                self.package()
        self.dependencies.assert_not_called()

    def test_runtime_change_before_publish_preserves_old_output(self):
        self.output.write_bytes(b"old kit")
        with patch.object(packager, "selected_runtime", side_effect=[
            {"kind": "source-built-cosmopolitan-runtime", "runtime_identity": "first"},
            {"kind": "source-built-cosmopolitan-runtime", "runtime_identity": "second"},
        ]):
            with self.assertRaisesRegex(ValueError, "runtime identity changed"):
                self.package()
        self.assertEqual(self.output.read_bytes(), b"old kit")
        self.assertFalse(list(self.root.glob(".kit.zip.*.tmp")))

    def test_verified_source_runtime_binding_is_recorded(self):
        runtime = {"kind": "source-built-cosmopolitan-runtime", "runtime_identity": "sha256:fixture",
                   "runtime_abi": "cosmopolitan-source-runtime-v1", "source": {"revision": "fixture"}}
        with patch.object(packager, "selected_runtime", return_value=runtime) as selected:
            self.assertEqual(self.package()["runtime"], runtime)
        self.assertEqual(selected.call_count, 2)

    def test_runtime_verification_retains_the_original_sdk_path(self):
        alias = self.root / "sdk-link"
        alias.symlink_to(self.sdk, target_is_directory=True)
        self.sdk = alias
        with patch.object(packager, "selected_runtime", return_value={"kind": "stock-sdk"}) as selected:
            self.package()
        self.assertEqual([call.args[0] for call in selected.call_args_list], [alias, alias])


class SourceDependencyCommandTest(unittest.TestCase):
    def test_each_raw_compiler_uses_its_mode_and_machin_flags(self):
        with tempfile.TemporaryDirectory(prefix="self-build-deps-") as temporary:
            repo = Path(temporary)
            source = repo / "compiler.c"
            source.write_text("fixture")
            for arch, mode in (("x86_64", ""), ("aarch64", "aarch64")):
                with self.subTest(arch=arch):
                    result = subprocess.CompletedProcess([], 0, stdout="compiler.o: compiler.c\n")
                    with patch.object(packager.subprocess, "run", return_value=result) as run:
                        actual = packager.source_dependencies(repo, repo / "sdk", source,
                                                              arch=arch, machin=True)
                    self.assertEqual(actual, [source])
                    command = run.call_args.args[0]
                    self.assertEqual(command[:2], ["sh", str(repo / f"sdk/bin/{arch}-linux-cosmo-gcc")])
                    self.assertIn(f'-DMODE="{mode}"', command)
                    for flag in ("-M", "-std=c11", "-pthread", "-Dmain=machin_entry"):
                        self.assertIn(flag, command)


@unittest.skipUnless(os.name == "posix", "Native helper fixtures require a POSIX host")
class NativeSelfBuildGuardTest(unittest.TestCase):
    """Compile a small host harness only when this deferred test suite is run."""

    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="self-build-guard-harness-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "cosmo.h").write_text("static int IsLinux(void) { return 0; }\n")
        source = root / "harness.c"
        source.write_text('#include ' + json.dumps(str(ROOT / "toolchain/self_build.c")) + r'''
static int tool_calls;
int embedded_sdk_materialize(const char *self,enum SdkComponent component) {
  (void)self;(void)component;return 0;
}
const char *embedded_sdk_root(void) {return getenv("SELF_BUILD_TEST_ROOT");}
int embedded_sdk_run(char *const argv[]) {
  tool_calls++;
  const char *name=strrchr(argv[0],'/');
  name=name?name+1:argv[0];
  if(!strcmp(name,"compile.ape")) {
    int n=0;while(argv[n])n++;
    return access(argv[n-1],R_OK)!=0;
  }
  if(!strcmp(name,"apelink")) {
    for(int i=1;argv[i];i++)if(!strcmp(argv[i],"-o")&&argv[i+1]) {
      FILE *file=fopen(argv[i+1],"wx");
      if(!file)return 1;
      fputs("fixture descendant bytes",file);return fclose(file)!=0;
    }
    return 1;
  }
  if(!strcmp(name,"zipcopy")) {
    const char *destination=getenv("SELF_BUILD_TEST_RACE_DEST");
    if(destination) {
      FILE *file=fopen(destination,"wx");
      if(!file)return 1;
      fputs("concurrent writer",file);return fclose(file)!=0;
    }
  }
  if(argv[1]&&!strcmp(argv[1],"selftest")) {
    const char *trace=getenv("SELF_BUILD_TEST_PROBE_TRACE");
    FILE *file=trace?fopen(trace,"a"):NULL;
    if(file){fputs("selftest\n",file);fclose(file);}
    return getenv("SELF_BUILD_TEST_REJECT_PROBE")?1:0;
  }
  return 0;
}
int main(int argc,char **argv) {
  if(argc>=3&&!strcmp(argv[1],"path"))return path_must_not_exist(argv[2])?1:0;
  if(argc==4&&!strcmp(argv[1],"compile")) {
    struct SourceSpec spec={"fixture",argv[3],atoi(argv[2])};
    int result=compile_object("/unused","x86_64",&spec);
    printf("%d %d\n",result,tool_calls);return 0;
  }
  if(argc>=3&&!strcmp(argv[1],"build"))return self_build_main(argc,argv);
  return 2;
}
''')
        cls.harness = root / "harness"
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
            "-std=gnu11", "-O0", "-I", str(root), str(source), "-o", str(cls.harness),
        ], check=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="self-build-guard-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kit = self.root / "sdk/self-build"
        for name in packager.SOURCE_NAMES:
            source = self.kit / "source/toolchain" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("fixture source, not compiled by the fake SDK")
        self.output = self.root / "child.com"
        self.sidecar = self.root / "child.com.provenance.json"
        self.trace = self.root / "probes.txt"
        sha = self.root / "sdk/bin/sha256sum.ape"
        sha.parent.mkdir()
        sha.write_text("#!/bin/sh\nprintf '%s\\n' '" + "a" * 64 + "'\n")
        sha.chmod(0o755)
        self.environment = os.environ.copy()
        for name in ("SELF_BUILD_TEST_RACE_DEST", "SELF_BUILD_TEST_REJECT_PROBE"):
            self.environment.pop(name, None)
        self.environment.update(SELF_BUILD_TEST_ROOT=str(self.root / "sdk"),
                                SELF_BUILD_TEST_PROBE_TRACE=str(self.trace))

    def invoke(self, *arguments):
        return subprocess.run([str(self.harness), *map(str, arguments)],
                              env=self.environment, text=True, capture_output=True)

    def test_optional_missing_source_skips_only_machin(self):
        missing = self.root / "missing.c"
        self.assertEqual(self.invoke("compile", "1", missing).stdout.strip(), "0 0")
        self.assertEqual(self.invoke("compile", "0", missing).stdout.strip(), "1 1")
        self.assertEqual(self.invoke("compile", "1", self.root).stdout.strip(), "1 0")
        missing.write_text("fixture source")
        self.assertEqual(self.invoke("compile", "1", missing).stdout.strip(), "0 2")
        self.assertEqual(self.invoke("compile", "1", missing / "child.c").stdout.strip(), "1 0")

    def test_existing_paths_including_dangling_symlinks_are_rejected(self):
        self.assertEqual(self.invoke("path", self.output).returncode, 0)
        self.output.symlink_to("missing-target")
        self.assertEqual(self.invoke("path", self.output).returncode, 1)
        self.assertNotEqual(self.invoke("build", self.output, "--compile-only").returncode, 0)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(os.readlink(self.output), "missing-target")

    def test_default_runs_dispatcher_probe_and_records_it(self):
        result = self.invoke("build", self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trace.read_text(), "selftest\n")
        self.assertEqual(json.loads(self.sidecar.read_text())["validation"], "dispatcher-selftest")

    def test_compile_only_does_not_execute_descendant_and_records_limit(self):
        self.environment["SELF_BUILD_TEST_REJECT_PROBE"] = "1"
        result = self.invoke("build", self.output, "--compile-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertEqual(json.loads(self.sidecar.read_text())["validation"], "compile-only")
        self.assertEqual(self.output.read_bytes(), b"fixture descendant bytes")

    def test_failed_default_probe_does_not_publish(self):
        self.environment["SELF_BUILD_TEST_REJECT_PROBE"] = "1"
        self.assertNotEqual(self.invoke("build", self.output).returncode, 0)
        self.assertEqual(self.trace.read_text(), "selftest\n")
        self.assertFalse(self.output.exists())
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(list(self.root.glob("*.tmp.*")))

    def test_unrecognized_or_extra_options_are_rejected(self):
        for options in (("--unknown",), ("--compile-only", "extra")):
            with self.subTest(options=options):
                self.assertEqual(self.invoke("build", self.output, *options).returncode, 2)
        self.assertFalse(self.output.exists())

    def test_concurrent_output_is_preserved(self):
        self.environment["SELF_BUILD_TEST_RACE_DEST"] = str(self.output)
        self.assertNotEqual(self.invoke("build", self.output, "--compile-only").returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"concurrent writer")
        self.assertFalse(self.sidecar.exists())
        self.assertFalse(list(self.root.glob("*.tmp.*")))

    def test_concurrent_sidecar_preserves_it_and_rolls_back_our_output(self):
        self.environment["SELF_BUILD_TEST_RACE_DEST"] = str(self.sidecar)
        self.assertNotEqual(self.invoke("build", self.output, "--compile-only").returncode, 0)
        self.assertEqual(self.sidecar.read_bytes(), b"concurrent writer")
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob("*.tmp.*")))


if __name__ == "__main__":
    unittest.main()
