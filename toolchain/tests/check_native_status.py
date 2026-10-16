"""Linux integration check: real status/argument handling with no writable /tmp."""

import argparse
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--ape-loader", type=Path, required=True)
    args = parser.parse_args()
    prefix = ["bwrap", "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/",
              "--tmpfs", "/tmp", "--remount-ro", "/tmp", "--dev", "/dev",
              "--proc", "/proc", str(args.ape_loader.resolve()),
              str(args.toolchain.resolve())]
    started = time.monotonic()
    result = subprocess.run(prefix + ["rust", "status"], capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    for expected in ("PASS: embedded Rust-APE source snapshot extracted",
                     "Resource access: ZIPOS (memory only; no SDK files extracted)",
                     "Rust compiler: packaged", "Cargo: packaged",
                     "Rust-APE patched target: packaged", "Patched Rust-APE SDK: packaged"):
        if expected not in result.stdout.splitlines():
            raise RuntimeError(f"Missing status line: {expected}\n{result.stdout}")
    print(result.stdout, end="")
    print(f"PASS: read-only status in {time.monotonic() - started:.3f}s")
    invalid = subprocess.run(prefix + ["rust", "not-a-command"], capture_output=True,
                             text=True, timeout=30)
    if invalid.returncode != 2 or "Usage:" not in invalid.stderr:
        raise RuntimeError("Invalid Rust command must fail before extraction: " + invalid.stderr)
    print("PASS: invalid command rejected without writable temporary storage")


if __name__ == "__main__":
    main()
