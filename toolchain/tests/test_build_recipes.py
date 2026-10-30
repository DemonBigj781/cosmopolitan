"""Keep packaged APE objects on one repository toolchain and ABI."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = ROOT / "toolchain/package-compiler-sdk.sh"
PACKAGE_MAKEFILE = ROOT / "toolchain/package-objects.mk"


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
            "rust_build",
            "tempdir",
        ):
            with self.subTest(name=name):
                self.assertIn(name, script)
        self.assertIn(
            "-Dmain=machin_entry",
            PACKAGE_MAKEFILE.read_text(),
        )


if __name__ == "__main__":
    unittest.main()
