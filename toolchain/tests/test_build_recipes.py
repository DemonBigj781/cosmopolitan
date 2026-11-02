"""Keep packaged APE objects on one repository toolchain and ABI."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = ROOT / "toolchain/package-compiler-sdk.sh"
PACKAGE_MAKEFILE = ROOT / "toolchain/package-objects.mk"
MACHIN_LINK_SCRIPT = ROOT / "toolchain/machin-link.sh"
SELFHOST_LINK_SCRIPT = ROOT / "toolchain/selfhost-link.sh"
MACHIN_BUILD_SOURCE = ROOT / "toolchain/machin_build.c"
SDK_WORKFLOW = ROOT / ".github/workflows/rust-ape-sdk-embed.yml"


class BuildRecipeTest(unittest.TestCase):
    def test_packaged_objects_use_repository_make_for_both_architectures(self):
        script = PACKAGE_SCRIPT.read_text()

        self.assertNotRegex(script, re.compile(r'"\$COSMOCC"[^\n]*\s-c\s'))
        self.assertIn(
            '"$COSMO_MAKE" -s -j1 -f Makefile '
            '-f toolchain/package-objects.mk MODE=',
            script,
        )
        self.assertIn(
            '"$COSMO_MAKE" -s -j1 -f Makefile '
            '-f toolchain/package-objects.mk MODE=aarch64',
            script,
        )
        for name in (
            "ape_toolchain",
            "embedded-sdk-driver",
            "native_zip",
            "machin_build",
            "project_manifest",
            "rust_build",
            "tempdir",
        ):
            with self.subTest(name=name):
                self.assertIn(name, script)
        package_makefile = PACKAGE_MAKEFILE.read_text()
        for flag in (
            "-Dmain=machin_entry",
            "-Wno-misleading-indentation",
            "-Wno-unused-variable",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, package_makefile)

    def test_generated_machin_sources_are_available_to_arm_make(self):
        package_makefile = PACKAGE_MAKEFILE.read_text()

        for name in ("compiler", "machin_demo"):
            with self.subTest(name=name):
                self.assertIn(
                    f"o/aarch64/toolchain/{name}.c: o//toolchain/{name}.c",
                    package_makefile,
                )

    def test_provenance_is_recorded_after_package_time_execution(self):
        script = PACKAGE_SCRIPT.read_text()
        provenance = script.index(
            "python3 toolchain/provenance.py "
            "o/toolchain/compiler-payload/toolchain-with-sdk.com"
        )

        for command in (
            "o/toolchain/compiler-payload/toolchain-with-sdk.com selftest",
            "o/toolchain/compiler-payload/toolchain-with-sdk.com lua -e 'print(6 * 7)'",
            "o/toolchain/compiler-payload/toolchain-with-sdk.com machin-compiler --full",
        ):
            with self.subTest(command=command):
                self.assertLess(script.index(command), provenance)

    def test_package_time_execution_uses_explicit_ape_loader(self):
        script = PACKAGE_SCRIPT.read_text()

        self.assertIn('APE_LOADER="$COSMOCC_ROOT/bin/ape-x86_64.elf"', script)
        self.assertEqual(
            script.count('COSMO_APE_LOADER="$APE_LOADER" "$APE_LOADER"'),
            3,
        )

    def test_packaging_workflow_preserves_universal_ape_during_smoke_tests(self):
        workflow = SDK_WORKFLOW.read_text()
        start = workflow.index("      - name: Check embedded SDK status")
        end = workflow.index("      - name: Prepare shared smoke-test artifact", start)
        smoke_step = workflow[start:end]

        self.assertIn('APE_LOADER=/tmp/cosmocc/bin/ape-x86_64.elf', smoke_step)
        self.assertEqual(
            smoke_step.count('COSMO_APE_LOADER="$APE_LOADER" "$APE_LOADER" "$APE"'),
            4,
        )

    def test_machin_subprocesses_reuse_explicit_ape_loader(self):
        source = MACHIN_BUILD_SOURCE.read_text()

        self.assertIn('getenv("COSMO_APE_LOADER")', source)
        self.assertIn("execv(loader,wrapped)", source)

    def test_machin_link_uses_repository_make_for_both_architectures(self):
        script = MACHIN_LINK_SCRIPT.read_text()

        self.assertNotRegex(script, re.compile(r'"\$COSMOCC"[^\n]*\s-c\s'))
        self.assertIn(
            '"$COSMO_MAKE" -s -j1 -f Makefile '
            '-f toolchain/package-objects.mk MODE=',
            script,
        )
        self.assertIn(
            '"$COSMO_MAKE" -s -j1 -f Makefile '
            '-f toolchain/package-objects.mk MODE=aarch64',
            script,
        )
        for path in (
            "o//toolchain/ape_toolchain.o",
            "o//toolchain/machin_demo.o",
            "o/aarch64/toolchain/ape_toolchain.o",
            "o/aarch64/toolchain/machin_demo.o",
        ):
            with self.subTest(path=path):
                self.assertIn(path, script)

    def test_selfhost_link_uses_repository_make_for_both_architectures(self):
        script = SELFHOST_LINK_SCRIPT.read_text()

        self.assertNotRegex(script, re.compile(r'"\$COSMOCC"[^\n]*\s-c\s'))
        self.assertIn(
            '"$COSMO_MAKE" -s -j1 -f Makefile '
            '-f toolchain/package-objects.mk MODE=',
            script,
        )
        self.assertIn(
            '"$COSMO_MAKE" -s -j1 -f Makefile '
            '-f toolchain/package-objects.mk MODE=aarch64',
            script,
        )
        for path in (
            "o//toolchain/ape_toolchain.o",
            "o//toolchain/compiler.o",
            "o/aarch64/toolchain/ape_toolchain.o",
            "o/aarch64/toolchain/compiler.o",
        ):
            with self.subTest(path=path):
                self.assertIn(path, script)


if __name__ == "__main__":
    unittest.main()
