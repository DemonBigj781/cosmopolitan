"""Portable SDK definitions are rendered from source templates, not repaired at runtime."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from rust_sdk_resources import portable_rust_resources


class RustSdkResourcesTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.sdk = self.root / "original-sdk"
        (self.sdk / "targets").mkdir(parents=True)
        (self.sdk / "scripts").mkdir()
        wrapper = self.sdk / "scripts/gcc-linker-wrapper.bash"
        wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "$ARCH" "$0" "$@"\n')
        wrapper.chmod(0o755)
        for arch in ("x86_64", "aarch64"):
            name = arch + "-unknown-linux-musl.json"
            source = ROOT / "third_party/rust_ape/upstream/targets" / name
            (self.sdk / "targets" / name).write_bytes(source.read_bytes())
            for tool in ("ar", "cc", "c++"):
                executable = self.sdk / "vendor/cosmocc/bin" / f"{arch}-unknown-cosmo-{tool}"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@"\n')
                executable.chmod(0o755)

    def render(self):
        resources = portable_rust_resources(self.sdk)
        for name, (data, mode) in resources.items():
            file = self.sdk / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            file.chmod(mode)
        return resources

    def test_generated_definitions_contain_no_build_root(self):
        resources = self.render()
        for name, (data, mode) in resources.items():
            self.assertNotIn(str(self.sdk).encode(), data, name)
            self.assertNotIn(b"@APE_LINKER@", data, name)
        for arch in ("x86_64", "aarch64"):
            target = json.loads(resources[f"generated/{arch}-unknown-linux-musl.json"][0])
            self.assertEqual(target["linker"], f"linker-{arch}.bash")

    def test_both_architecture_wrappers_work_after_sdk_move(self):
        self.render()
        old = str(self.sdk)
        moved = self.root / "relocated sdk"
        self.sdk.rename(moved)
        self.assertFalse(self.sdk.exists())
        for arch in ("x86_64", "aarch64"):
            for tool in ("linker", "ar", "cc", "cxx"):
                with self.subTest(arch=arch, tool=tool):
                    result = subprocess.run([str(moved / "generated" / f"{tool}-{arch}.bash"),
                                             "argument with spaces"], cwd=self.root,
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(str(moved), result.stdout)
                    self.assertNotIn(old, result.stdout)
                    self.assertIn("argument with spaces", result.stdout.splitlines())
                    if tool == "linker":
                        self.assertEqual(result.stdout.splitlines()[0], arch)

    def test_cargo_paths_use_their_correct_configuration_bases(self):
        resources = self.render()
        config_path = self.sdk / "generated/cargo.cosmo.toml"
        config = tomllib.loads(resources["generated/cargo.cosmo.toml"][0].decode())
        for arch in ("x86_64", "aarch64"):
            target = config["target"][f"{arch}-unknown-linux-musl"]
            self.assertEqual((config_path.parent.parent / target["linker"]).resolve(),
                             (self.sdk / "generated" / f"linker-{arch}.bash").resolve())
        for name in ("libc", "errno"):
            path = config["patch"]["crates-io"][name]["path"]
            self.assertEqual((config_path.parent.parent / path).resolve(),
                             (self.sdk / "vendor/patches" / name).resolve())
        source = config["env"]["__CARGO_TESTS_ONLY_SRC_ROOT"]
        self.assertTrue(source["relative"])
        self.assertEqual(source["value"], "vendor/library")

    def test_unknown_target_template_is_not_silently_rewritten(self):
        target = self.sdk / "targets/x86_64-unknown-linux-musl.json"
        value = json.loads(target.read_text())
        value["linker"] = "/unexpected/tool"
        target.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "linker placeholder"):
            self.render()


if __name__ == "__main__":
    unittest.main()
