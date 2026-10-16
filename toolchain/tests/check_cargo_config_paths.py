"""Check relocated patch paths with the pinned Cargo, without compiling code."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from rust_sdk_resources import portable_rust_resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cargo-path-check-") as workspace:
        root = Path(workspace)
        sdk = root / "original-sdk"
        (sdk / "targets").mkdir(parents=True)
        (sdk / "scripts").mkdir()
        (sdk / "scripts/gcc-linker-wrapper.bash").write_text("#!/bin/sh\nexit 1\n")
        for target in (ROOT / "third_party/rust_ape/upstream/targets").glob("*.json"):
            (sdk / "targets" / target.name).write_bytes(target.read_bytes())
        for name, version in (("libc", "0.2.189"), ("errno", "0.3.14")):
            crate = sdk / "vendor/patches" / name
            (crate / "src").mkdir(parents=True)
            (crate / "src/lib.rs").write_text("")
            (crate / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "{version}"\nedition = "2021"\n')
        for name, (data, mode) in portable_rust_resources(sdk).items():
            path = sdk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        moved = root / "moved-sdk"
        sdk.rename(moved)
        project = root / "consumer"
        (project / "src").mkdir(parents=True)
        (project / "src/lib.rs").write_text("")
        (project / "Cargo.toml").write_text(
            '[package]\nname = "path_probe"\nversion = "0.1.0"\nedition = "2021"\n'
            '[dependencies]\nlibc = "=0.2.189"\nerrno = "=0.3.14"\n')
        command = [str(args.cargo.resolve()), "--config", str(moved / "generated/cargo.cosmo.toml"),
                   "metadata", "--offline", "--format-version", "1",
                   "--manifest-path", str(project / "Cargo.toml")]
        env = dict(os.environ, CARGO_HOME=str(root / "cargo-home"), RUSTC=str(args.rustc.resolve()))
        result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        packages = {p["name"]: p for p in json.loads(result.stdout)["packages"]}
        for name in ("libc", "errno"):
            expected = moved / "vendor/patches" / name / "Cargo.toml"
            if Path(packages[name]["manifest_path"]).resolve() != expected.resolve():
                raise RuntimeError(f"Cargo did not use relocated {name}")
        if sdk.exists():
            raise RuntimeError("Original SDK unexpectedly exists")
        print("PASS: pinned Cargo resolves relocated patch paths from an unrelated directory")


if __name__ == "__main__":
    main()
