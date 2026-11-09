"""Deferred recursive-kit inventory checks; no target execution in these tests."""
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_recursive_self_build import verify_self_build_inventory


class RecursiveInventoryTest(unittest.TestCase):
    def test_manifest_must_describe_the_archived_bytes(self):
        name = "self-build/source/toolchain/main.c"
        data = b"source bytes"
        digest = hashlib.sha256(data).hexdigest()
        record = {"entry_count": 1, "entries": [
            {"path": name, "size": len(data), "mode": 0o644, "sha256": digest}],
            "kit_tree_sha256": hashlib.sha256(f"{digest} {len(data)} 644 {name}\n".encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "kit.zip"
            for content, expected_failure in ((data, False), (b"altered bytes", True)):
                with zipfile.ZipFile(path, "w") as archive:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, content)
                    archive.writestr("self-build/manifest.json", json.dumps(record))
                with zipfile.ZipFile(path) as archive:
                    if expected_failure:
                        with self.assertRaises(ValueError):
                            verify_self_build_inventory(archive)
                    else:
                        verify_self_build_inventory(archive)


if __name__ == "__main__":
    unittest.main()
