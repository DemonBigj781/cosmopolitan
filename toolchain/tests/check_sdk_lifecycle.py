"""Integration check for cleanup after a real compiler failure, not just success."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from smoke_compilers import OWNED_TEMP_PREFIXES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--ape-loader", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sdk-failure-check-") as workspace:
        root = Path(workspace)
        scratch = root / "scratch"
        scratch.mkdir()
        sentinel = scratch / "keep.txt"
        sentinel.write_text("unrelated caller data")
        command = [str(args.ape_loader.resolve()), str(args.toolchain.resolve()),
                   "cc", str(root / "missing.c"), "-o", str(root / "output.com")]
        result = subprocess.run(command, env=dict(os.environ, TMPDIR=str(scratch)),
                                capture_output=True, text=True, timeout=600)
        if result.returncode == 0 or "missing.c" not in result.stderr:
            raise RuntimeError("Expected a genuine compiler input failure: " + result.stderr)
        if "SDK materialized: C" not in result.stderr:
            raise RuntimeError("Compiler test never materialized its SDK: " + result.stderr)
        remaining = [p.name for p in scratch.iterdir() if p.name.startswith(OWNED_TEMP_PREFIXES)]
        if remaining or sentinel.read_text() != "unrelated caller data":
            raise RuntimeError(f"Incorrect failure cleanup: {remaining}")
        print("PASS: compiler failure cleaned its own SDK and preserved caller data")


if __name__ == "__main__":
    main()
