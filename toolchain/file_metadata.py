"""Streaming fingerprints; tree hashes cover names, modes, bytes and link targets."""

import hashlib
import json
import os
from pathlib import Path
import stat

STAMP = ".toolchain-bootstrap.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    # Only root metadata is excluded; nested .git/stamp files remain covered.
    # Links are hashed as links, never traversed, including directory symlinks.
    digest = hashlib.sha256()

    def visit(directory):
        for path in sorted(directory.iterdir()):
            if directory == root and path.name in (STAMP, ".git"):
                continue
            mode = path.lstat().st_mode
            name = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                value = [name, "link", os.readlink(path)]
            elif stat.S_ISREG(mode):
                value = [name, "file", stat.S_IMODE(mode), sha256(path)]
            elif stat.S_ISDIR(mode):
                value = [name, "directory", stat.S_IMODE(mode)]
            else:
                raise ValueError(f"Unsupported file type: {path}")
            digest.update((json.dumps(value, ensure_ascii=True) + "\n").encode())
            if stat.S_ISDIR(mode):
                visit(path)

    visit(root)
    return digest.hexdigest()
