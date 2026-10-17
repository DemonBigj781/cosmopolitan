"""Package Linux-cross-compiled Rust programs using the pinned Rust-APE recipe.

This links build outputs only. It performs no macOS execution or compatibility
review and never retries with a reduced set of requested bootstrap inputs.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from file_metadata import sha256

ARCHITECTURES = ("x86_64", "aarch64")


def compiled_input(target_dir, architecture, program):
    directory = target_dir / f"{architecture}-unknown-linux-musl" / "release"
    for name in (f"{program}.com.dbg", program):
        path = directory / name
        if path.is_file():
            if not path.stat().st_size:
                raise ValueError(f"Empty compiled input: {path}")
            return path
    raise FileNotFoundError(f"No compiled {architecture} input for {program} in {directory}")


def link_programs(sdk, target_dir, programs, output, source_commit):
    sdk, target_dir, output = sdk.resolve(), target_dir.resolve(), output.resolve()
    names = [name for name, _ in programs]
    files = [file for _, file in programs]
    if not programs or len(names) != len(set(names)) or len(files) != len(set(files)):
        raise ValueError("Provide a nonempty, unique program list")
    for name in names + files:
        if not name or name in (".", "..") or Path(name).name != name or "\\" in name:
            raise ValueError(f"Invalid program name: {name}")
    if set(files) & {"BUILD.json", "SHA256SUMS", "LICENSE.rust-ape"}:
        raise ValueError("Program output collides with build metadata")
    tools = sdk / "vendor/cosmocc/bin"
    required = [tools / name for name in
                ("apelink", "pecheck", "ape-x86_64.elf", "ape-aarch64.elf", "ape-m1.c")]
    required.append(sdk / "LICENSE")
    inputs = {name: [compiled_input(target_dir, arch, name) for arch in ARCHITECTURES]
              for name in names}
    for path in required:
        if not path.is_file() or not path.stat().st_size:
            raise FileNotFoundError(f"Required build input missing: {path}")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "source_commit": source_commit,
        "scope": "Rust-generated programs; not rustc/Cargo host portability",
        "architectures": list(ARCHITECTURES),
        "macos": {"compile_only": True, "runtime_tested": False,
                  "compatibility_reviewed": False, "third_party_review_required": True},
        "arm64_runtime_tested": False,
        "programs": [],
    }
    for name, filename in programs:
        staged = output / f"{filename}.tmp"
        final = output / filename
        command = ["sh", str(tools / "apelink"),
                   "-l", str(tools / "ape-x86_64.elf"),
                   "-l", str(tools / "ape-aarch64.elf"),
                   "-M", str(tools / "ape-m1.c"), "-o", str(staged),
                   *[str(path) for path in inputs[name]]]
        subprocess.run(command, check=True)
        if not staged.is_file() or not staged.stat().st_size:
            raise RuntimeError(f"Linker produced no output for {name}")
        subprocess.run(["sh", str(tools / "pecheck"), str(staged)], check=True)
        staged.chmod(0o755)
        os.replace(staged, final)
        manifest["programs"].append({
            "name": name, "file": final.name, "sha256": sha256(final),
            "input_sha256": {arch: sha256(path) for arch, path in zip(ARCHITECTURES, inputs[name])},
        })
    shutil.copyfile(sdk / "LICENSE", output / "LICENSE.rust-ape")
    (output / "BUILD.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(
        f"{program['sha256']}  {program['file']}\n" for program in manifest["programs"]))
    print("Built dual-architecture Rust APE programs; macOS untested/unreviewed, ARM64 runtime deferred")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--program", action="append", nargs=2, required=True,
                        metavar=("CARGO_BINARY", "OUTPUT_FILE"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    link_programs(args.sdk, args.target_dir, args.program, args.output_dir, args.source_commit)


if __name__ == "__main__":
    main()
