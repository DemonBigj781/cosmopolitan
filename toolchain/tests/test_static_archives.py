"""Deferred archive closure tests; run only after the implementation gate."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from static_archives import archive_hashes, verify_archives


def member(name, data):
    header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return header + data + (b"\n" if len(data) % 2 else b"")


class StaticArchivesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "library.a"
        self.path.write_bytes(b"!<arch>\n" + member("unit.o/", b"nonempty fixture object"))

    def test_exact_hash_and_member_closure(self):
        expected = archive_hashes([self.path])
        self.assertEqual(verify_archives([self.path], expected), expected)
        with self.assertRaises(ValueError):
            verify_archives([self.path], {})
        self.path.write_bytes(b"!<arch>\n" + member("unit.o/", b"different object"))
        with self.assertRaises(ValueError):
            verify_archives([self.path], expected)

    def test_thin_empty_and_truncated_archives(self):
        for data in (b"!<thin>\n", b"!<arch>\n", b"!<arch>\n" + member("/", b"symbols"),
                     b"!<arch>\n" + member("unit.o/", b""),
                     b"!<arch>\n" + member("unit.o/", b"odd")[:-1]):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    archive_hashes([self.path])

    def test_bsd_extended_name_and_gnu_name_table(self):
        self.path.write_bytes(b"!<arch>\n" + member("#1/6", b"unit.oobject"))
        archive_hashes([self.path])
        self.path.write_bytes(b"!<arch>\n" + member("//", b"long-file-name.o/\n") + member("/0", b"object"))
        archive_hashes([self.path])


if __name__ == "__main__":
    unittest.main()
