"""Verify complete ordinary static archives and their recorded content closure."""
import hashlib
import os
from pathlib import Path
import re


def archive_sha256(path):
    path = Path(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if stream.read(8) != b"!<arch>\n":
            raise ValueError(f"Expected ordinary static archive, not thin/shared input: {path}")
        members = 0
        name_table = None
        long_names = []
        while True:
            header = stream.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:] != b"`\n":
                raise ValueError(f"Malformed archive member header: {path}")
            size_text = header[48:58].strip()
            if not size_text.isdigit():
                raise ValueError(f"Malformed archive member size: {path}")
            size = int(size_text)
            start = stream.tell()
            if size > before.st_size - start:
                raise ValueError(f"Truncated static archive member: {path}")
            name = header[:16].strip()
            if not name:
                raise ValueError(f"Unnamed archive member: {path}")
            payload_size = size
            bsd_name = False
            if name.startswith(b"#1/"):
                bsd_name = True
                length = name[3:]
                if not length.isdigit() or int(length) > size:
                    raise ValueError(f"Malformed BSD archive member name: {path}")
                extended = stream.read(int(length)).rstrip(b"\0")
                payload_size -= int(length)
                name = extended
            if not bsd_name and name == b"//":
                if name_table is not None:
                    raise ValueError(f"Duplicate GNU archive name table: {path}")
                name_table = stream.read(payload_size)
            elif not bsd_name and name.startswith(b"/") and name not in (b"/", b"/SYM64/"):
                if not name[1:].isdigit():
                    raise ValueError(f"Invalid GNU archive member name: {path}")
                long_names.append(int(name[1:]))
            if name not in (b"/", b"//", b"/SYM64/") and not name.startswith(b"__.SYMDEF"):
                if not name or payload_size == 0:
                    raise ValueError(f"Empty object member in static archive: {path}")
                members += 1
            stream.seek(start + size)
            if size % 2 and stream.read(1) != b"\n":
                raise ValueError(f"Missing static archive member padding: {path}")
        if not members:
            raise ValueError(f"Static archive has no object members: {path}")
        for position in long_names:
            if name_table is None or position >= len(name_table):
                raise ValueError(f"GNU archive member has no name-table entry: {path}")
            if position and name_table[position - 1:position] != b"\n":
                raise ValueError(f"GNU archive name offset is not an entry boundary: {path}")
            end = name_table.find(b"/\n", position)
            if end <= position or b"\n" in name_table[position:end] or b"\0" in name_table[position:end]:
                raise ValueError(f"Invalid GNU archive name terminator: {path}")
        stream.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"Static archive changed while verifying: {path}")
        return digest.hexdigest()


def archive_hashes(paths):
    return {str(path): archive_sha256(path) for path in paths}


def verify_archives(paths, expected, required=True):
    paths = list(paths)
    keys = {str(path) for path in paths}
    if expected is None and not required:
        return archive_hashes(paths)
    if (not isinstance(expected, dict) or set(expected) != keys or
            any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in expected.values())):
        raise ValueError("Static archive hash map does not cover the exact link inputs")
    actual = archive_hashes(paths)
    if actual != expected:
        raise ValueError("Static archive bytes do not match completed build metadata")
    return actual
