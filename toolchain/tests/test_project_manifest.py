"""Focused contract tests for the JSON project manifest driver."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProjectManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        cls.executable = Path(cls.build.name) / "project-manifest-test.elf"
        environment = dict(
            os.environ,
            MAKEFLAGS="-j1",
            CMAKE_BUILD_PARALLEL_LEVEL="1",
            CARGO_BUILD_JOBS="1",
        )
        subprocess.run(
            [
                "make",
                "-s",
                "-j1",
                "MODE=",
                "o//toolchain/project_manifest.o",
                "o//toolchain/tempdir.o",
                "o//toolchain/tests/project_manifest_test.o",
                "o//tool/net/ljson.o",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        lua_deps = subprocess.run(
            [
                "make",
                "-s",
                "-j1",
                "-f",
                "Makefile",
                "-f",
                "toolchain/lua-link.mk",
                "toolchain-lua-link",
                "MODE=",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        subprocess.run(
            [
                "make",
                "-s",
                "-j1",
                "-f",
                "Makefile",
                "-f",
                "toolchain/unified-link.mk",
                "MODE=",
                f"TOOLCHAIN_OUTPUT={cls.executable}",
                "TOOLCHAIN_OBJECTS=o//toolchain/project_manifest.o "
                "o//toolchain/tests/project_manifest_test.o o//toolchain/tempdir.o o//tool/net/ljson.o",
                f"TOOLCHAIN_DEPS={lua_deps}",
                str(cls.executable),
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.sdk = self.root / "sdk"
        self.sdk.mkdir()
        (self.sdk / "bin").mkdir()
        for name in (
            "x86_64-unknown-cosmo-cc",
            "x86_64-unknown-cosmo-c++",
            "apelink",
            "ape-x86_64.elf",
        ):
            path = self.sdk / "bin" / name
            path.write_text("fixture\n")
            path.chmod(0o755)
        self.log = self.root / "arguments.log"
        self.frontend_log = self.root / "frontend.log"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        (self.project / "main.c").write_text("int main(void) { return 0; }\n")

    def run_manifest(self, manifest, *, raw=False, extra_env=None):
        path = self.project / "manifest.json"
        path.write_text(manifest if raw else json.dumps(manifest))
        environment = dict(
            os.environ,
            TEST_SDK_ROOT=str(self.sdk),
            TEST_ARGUMENT_LOG=str(self.log),
            TEST_FRONTEND_LOG=str(self.frontend_log),
            TMPDIR=str(self.scratch),
            **(extra_env or {}),
        )
        return subprocess.run(
            [str(self.executable), str(path)],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
        )

    @staticmethod
    def manifest(**updates):
        value = {
            "language": "c",
            "target": "ape",
            "output": "build/student.com",
            "sources": "main.c",
        }
        value.update(updates)
        return {"manifest": value}

    def arguments(self):
        return self.log.read_text().splitlines()

    def test_valid_c_manifest_builds_output(self):
        result = self.run_manifest(self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.project / "build/student.com").read_text(), "student output\n"
        )
        self.assertIn(str((self.project / "main.c").resolve()), self.arguments())

    def test_x86_64_target_uses_architecture_specific_compiler(self):
        result = self.run_manifest(self.manifest(target="x86_64"))
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.arguments()
        self.assertEqual(
            arguments[0], str(self.sdk / "bin/x86_64-unknown-cosmo-cc")
        )
        self.assertNotIn("-mcosmo", arguments)
        self.assertNotIn("c", arguments[:2])

    def test_string_and_array_forms_resolve_relative_to_manifest(self):
        (self.project / "extra.c").write_text("int extra(void) { return 1; }\n")
        (self.project / "one.a").write_text("one")
        (self.project / "two.a").write_text("two")
        bundle = self.project / "bundle"
        (bundle / "include").mkdir(parents=True)
        (bundle / "lib").mkdir()
        (bundle / "lib/library.a").write_text("library")
        (bundle / "link.args").write_text("lib/library.a\n-DBUNDLE=1\n")
        result = self.run_manifest(
            self.manifest(
                sources=["main.c", "extra.c"],
                libraries={
                    "self": [],
                    "file": ["one.a", "two.a"],
                    "folder": "bundle",
                    "git": [],
                    "ftp": [],
                    "http": [],
                    "https": [],
                },
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.arguments()
        for path in ("main.c", "extra.c", "one.a", "two.a"):
            self.assertIn(str((self.project / path).resolve()), arguments)
        self.assertIn(str((bundle / "include").resolve()), arguments)
        self.assertIn(str((bundle / "lib/library.a").resolve()), arguments)
        self.assertIn("-DBUNDLE=1", arguments)

    def test_malformed_json_is_rejected(self):
        result = self.run_manifest('{"manifest":', raw=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid JSON manifest", result.stderr)

    def test_missing_required_fields_are_rejected(self):
        for field in ("language", "target", "output", "sources"):
            with self.subTest(field=field):
                manifest = self.manifest()
                del manifest["manifest"][field]
                result = self.run_manifest(manifest)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"manifest.{field} is required", result.stderr)

    def test_unknown_fields_are_rejected_at_each_object_level(self):
        cases = [
            ({"manifest": self.manifest()["manifest"], "extra": True}, "JSON root.extra"),
            (self.manifest(extra=True), "manifest.extra"),
            (self.manifest(libraries={"local": "library.a"}), "manifest.libraries.local"),
        ]
        for manifest, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                result = self.run_manifest(manifest)
                self.assertEqual(result.returncode, 2)
                self.assertIn(diagnostic, result.stderr)

    def test_unknown_self_library_is_rejected(self):
        result = self.run_manifest(self.manifest(libraries={"self": "opengl"}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown embedded toolchain library: opengl", result.stderr)

    def test_webgpu_requires_x86_64_target(self):
        result = self.run_manifest(self.manifest(libraries={"self": "webgpu"}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("webgpu currently requires manifest.target 'x86_64'", result.stderr)

    def test_remote_library_sources_fail_explicitly(self):
        for source in ("git", "ftp", "http", "https"):
            with self.subTest(source=source):
                result = self.run_manifest(
                    self.manifest(libraries={source: f"{source}://example.invalid/library"})
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    f"manifest library source '{source}' is recognized", result.stderr
                )

    def test_language_frontends_use_shared_local_library_linker(self):
        (self.project / "one.a").write_text("archive fixture")
        bundle = self.project / "bundle"
        (bundle / "include").mkdir(parents=True)
        (bundle / "lib").mkdir()
        (bundle / "lib/two.a").write_text("archive fixture")
        (bundle / "link.args").write_text("lib/two.a\n-lm\n")
        for language in ("rust", "machin"):
            with self.subTest(language=language):
                result = self.run_manifest(self.manifest(
                    language=language, libraries={"file": "one.a", "folder": "bundle"}))
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = self.arguments()
                self.assertIn(str(self.project / "one.a"), arguments)
                self.assertIn(str(bundle / "lib/two.a"), arguments)
                self.assertIn(str(bundle / "include"), arguments)
                self.assertEqual(self.frontend_log.read_text().splitlines()[0], language)
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_rust_manifest_selects_required_object_architectures(self):
        for target, multiarch in (("ape", "1"), ("x86_64", "0")):
            with self.subTest(target=target):
                result = self.run_manifest(self.manifest(language="rust", target=target))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.frontend_log.read_text().splitlines()[3], multiarch)
                self.assertTrue(any(argument.endswith("/rust.o") for argument in self.arguments()))
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_language_webgpu_uses_shared_runtime_and_stack_policy(self):
        webgpu = self.sdk / "webgpu"
        webgpu.mkdir()
        (webgpu / "link.args").write_text("lib/wgpu.a\n")
        for language in ("rust", "machin"):
            with self.subTest(language=language):
                result = self.run_manifest(self.manifest(
                    language=language, target="x86_64", libraries={"self": "webgpu"}),
                    extra_env={"TEST_PE_OUTPUT": "1"})
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = self.arguments()
                self.assertIn(str(webgpu / "lib/wgpu.a"), arguments)
                self.assertIn(str(webgpu / "runtime/vulkan_loader.c"), arguments)
                self.assertIn(str(webgpu / "runtime/win64_bridge.c"), arguments)
                image = (self.project / "build/student.com").read_bytes()
                self.assertEqual(int.from_bytes(image[160:168], "little"), 8 * 1024 * 1024)
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_compiler_sidecars_are_removed_for_every_language(self):
        for language in ("c", "c++", "rust", "machin"):
            for fail in ("0", "9"):
                with self.subTest(language=language, failure=fail):
                    result = self.run_manifest(self.manifest(language=language), extra_env={
                        "TEST_COMPILER_SIDECARS": "1", "TEST_COMPILER_FAIL": fail})
                    self.assertEqual(result.returncode, int(fail), result.stderr)
                    self.assertEqual(list((self.project / "build").glob("*.cosmo-tmp-*")), [])
                    self.assertEqual(list(self.scratch.iterdir()), [])

    def test_language_frontend_failure_preserves_output_and_cleans_workspace(self):
        output = self.project / "build/student.com"
        output.parent.mkdir()
        output.write_text("old output\n")
        for language in ("rust", "machin"):
            with self.subTest(language=language):
                result = self.run_manifest(self.manifest(language=language),
                                           extra_env={"TEST_FRONTEND_FAIL": "7"})
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(output.read_text(), "old output\n")
                self.assertFalse(self.log.exists())
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_language_link_failure_preserves_output_and_cleans_workspace(self):
        output = self.project / "build/student.com"
        output.parent.mkdir()
        output.write_text("old output\n")
        for language in ("rust", "machin"):
            with self.subTest(language=language):
                result = self.run_manifest(self.manifest(language=language),
                                           extra_env={"TEST_COMPILER_FAIL": "9"})
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertEqual(output.read_text(), "old output\n")
                self.assertEqual(list(output.parent.glob("student.com.cosmo-tmp-*")), [])
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_language_constraints_fail_before_frontend_execution(self):
        for language in ("rust", "machin"):
            for updates in ({"target": "invalid"},
                            {"sources": ["main.c", "main.c"]},
                            {"libraries": {"self": "webgpu"}},
                            {"libraries": {"self": "unknown"}},
                            {"libraries": {"https": "https://example.invalid/lib.a"}}):
                with self.subTest(language=language, updates=updates):
                    result = self.run_manifest(self.manifest(language=language, **updates))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse(self.frontend_log.exists())
                    self.assertFalse(self.log.exists())
                    self.assertEqual(list(self.scratch.iterdir()), [])

    def test_existing_regular_output_is_replaced_without_backup_residue(self):
        output = self.project / "build/student.com"
        output.parent.mkdir()
        output.write_text("old output\n")
        result = self.run_manifest(self.manifest())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), "student output\n")
        self.assertEqual(list(output.parent.glob("student.com.cosmo-backup-*")), [])

    def test_compiler_failure_leaves_no_partial_output(self):
        output = self.project / "build/student.com"
        output.parent.mkdir()
        output.write_text("old output\n")
        result = self.run_manifest(
            self.manifest(), extra_env={"TEST_COMPILER_FAIL": "9"}
        )
        self.assertEqual(result.returncode, 9)
        self.assertEqual(output.read_text(), "old output\n")
        self.assertEqual(list((self.project / "build").glob("student.com.cosmo-tmp-*")), [])

    def test_symlink_output_is_not_replaced(self):
        target = self.root / "outside.com"
        target.write_text("outside output\n")
        output = self.project / "build/student.com"
        output.parent.mkdir()
        output.symlink_to(target)
        result = self.run_manifest(self.manifest())
        self.assertEqual(result.returncode, 1)
        self.assertIn("publish student APE", result.stderr)
        self.assertTrue(output.is_symlink())
        self.assertEqual(target.read_text(), "outside output\n")
        self.assertEqual(list(output.parent.glob("student.com.cosmo-tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
