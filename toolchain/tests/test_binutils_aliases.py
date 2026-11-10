"""Check source binutils aliases and the real copy recipe without a build."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from runtime_identity import (ARCHITECTURES, BINUTILS_ALIASES, inventory,
                              required_files, require_layout)
from export_foundation import materialize_sdk, verify_materialized_sdk


class BinutilsAliasTest(unittest.TestCase):
    def test_tracked_source_aliases_select_the_matching_target_tool(self):
        tools = ("addr2line", "ar", "as", "c++filt", "nm", "objcopy", "objdump", "readelf", "strip")
        expected = {f"bin/{arch}-unknown-cosmo-{tool}": f"{arch}-linux-cosmo-{tool}"
                    for arch in ARCHITECTURES for tool in tools}
        self.assertEqual(BINUTILS_ALIASES, expected)
        self.assertEqual(len(expected), 18)
        for name, target in expected.items():
            with self.subTest(alias=name):
                alias = ROOT / "tool/cosmocc" / name
                self.assertTrue(stat.S_ISLNK(alias.lstat().st_mode))
                self.assertEqual(os.readlink(alias), target)
                self.assertIn(name, required_files())
                self.assertIn("bin/" + target, required_files())

    def test_package_copy_preserves_all_aliases_and_resolves_their_targets(self):
        recipe = (ROOT / "tool/cosmocc/package.sh").read_text().splitlines()
        copies = [line for line in recipe if "tool/cosmocc/bin/*" in line]
        self.assertEqual(copies, ['cp -af tool/cosmocc/bin/* "$OUTDIR/bin/"'])
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary) / "sdk"
            for name in required_files():
                if name not in BINUTILS_ALIASES:
                    path = sdk / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"synthetic packaged file, not an executable")
            # Execute only the producer's copy step, never a compiler or tool.
            subprocess.run(["sh", "-c", copies[0]], cwd=ROOT,
                           env={"PATH": os.defpath, "OUTDIR": str(sdk)}, check=True)
            require_layout(sdk)
            files = inventory(sdk)
            for name, target in BINUTILS_ALIASES.items():
                with self.subTest(alias=name):
                    alias = sdk / name
                    self.assertTrue(alias.is_symlink())
                    self.assertEqual(os.readlink(alias), target)
                    self.assertEqual(alias.resolve(strict=True), (sdk / "bin" / target).resolve(strict=True))
                    self.assertEqual(files[name]["type"], "symlink")
                    self.assertEqual(files[name]["target"], target)
                    self.assertEqual(alias.read_bytes(), (sdk / "bin" / target).read_bytes())
                    target_path = sdk / "bin" / target
                    data = target_path.read_bytes()
                    target_path.unlink()
                    with self.assertRaises(ValueError) as error:
                        require_layout(sdk)
                    self.assertEqual(str(error.exception), "Incomplete source runtime SDK: bin/" + target)
                    target_path.write_bytes(data)
            # Export intentionally expands the original SDK's aliases. Its
            # separate verifier must accept regular copies and preserve the
            # original SOURCE_RUNTIME bytes, not reapply source layout rules.
            record = {"files": files}
            raw = json.dumps(record, sort_keys=True).encode() + b"\n"
            (sdk / "SOURCE_RUNTIME.json").write_bytes(raw)
            exported = Path(temporary) / "exported"
            materialize_sdk(sdk, exported, record)
            verify_materialized_sdk(exported, record)
            self.assertEqual((exported / "SOURCE_RUNTIME.json").read_bytes(), raw)
            for name, target in BINUTILS_ALIASES.items():
                self.assertFalse((exported / name).is_symlink())
                self.assertEqual((exported / name).read_bytes(), (exported / "bin" / target).read_bytes())


if __name__ == "__main__":
    unittest.main()
