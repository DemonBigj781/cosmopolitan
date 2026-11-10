#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bind actual Cargo invocation/artifact evidence to the completed WebGPU build."""
import argparse
import json
from pathlib import Path
import sys

from source_receipt import file_sha256, parse_json, regular_bytes, sha256


def verify_cargo(output, sdk, runtime, archive, expected_invocation_sha256=None,
                 expected_artifacts_sha256=None):
    receipt_path = output / "cargo-invocation.json"
    invocation_sha256 = file_sha256(receipt_path)
    if expected_invocation_sha256 is not None and invocation_sha256 != expected_invocation_sha256:
        raise ValueError("Cargo invocation receipt changed during the WebGPU build")
    recipe = parse_json(regular_bytes(receipt_path))
    if (recipe.get("schema_version") != 1 or recipe.get("status") != "invocation-only"
            or recipe.get("runtime") != runtime):
        raise ValueError("Cargo invocation receipt does not match the selected runtime")
    wrapper = output.parent.parent / "third_party/rust_ape/run.sh"
    if file_sha256(wrapper) != recipe["wrapper_sha256"]:
        raise ValueError("Rust Cargo wrapper changed during the WebGPU build")
    for path, expected in recipe["config_sha256"].items():
        if file_sha256(Path(path)) != expected:
            raise ValueError(f"Cargo configuration changed: {path}")
    target = sdk / "generated/x86_64-unknown-linux-musl.json"
    if (recipe["environment"].get("CARGO_BUILD_TARGET") != str(target) or
            file_sha256(target) != recipe["target_sha256"]):
        raise ValueError("Cargo target does not match the selected SDK")
    raw = regular_bytes(output / "cargo-artifacts.jsonl")
    artifacts_sha256 = sha256(raw)
    if expected_artifacts_sha256 is not None and artifacts_sha256 != expected_artifacts_sha256:
        raise ValueError("Cargo artifact log changed during the WebGPU build")
    selected = []
    finished = False
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue  # Wrapper setup diagnostics are not Cargo JSON messages.
        if not isinstance(record, dict):
            continue
        if record.get("reason") == "build-finished":
            finished = record.get("success") is True
        if (record.get("reason") == "compiler-artifact" and
                Path(record.get("manifest_path", "")).resolve() == (output / "source/Cargo.toml").resolve()
                and record.get("target", {}).get("name") == "wgpu_native"
                and "staticlib" in record.get("target", {}).get("crate_types", [])):
            selected.extend(Path(name) for name in record.get("filenames", []) if name.endswith(".a"))
    if not finished or len(selected) != 1 or selected[0].resolve() != archive.resolve():
        raise ValueError("Cargo did not report a successful build of the selected WebGPU staticlib")
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("The selected WebGPU staticlib is not a regular archive")
    return {
        "cargo_invocation_sha256": invocation_sha256,
        "cargo_artifacts_sha256": artifacts_sha256,
        "cargo_invocation": recipe,
        "selected_staticlib": str(archive),
    }


def record_command(path, phase, argv):
    records = json.loads(path.read_text()) if path.exists() else {}
    if phase in records:
        raise ValueError(f"Duplicate build recipe phase: {phase}")
    records[phase] = {"argv": argv}
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record-command")
    record.add_argument("path", type=Path)
    record.add_argument("phase")
    record.add_argument("argv", nargs=argparse.REMAINDER)
    verify = subparsers.add_parser("verify-cargo")
    verify.add_argument("output", type=Path)
    verify.add_argument("sdk", type=Path)
    verify.add_argument("runtime")
    verify.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "record-command":
        record_command(args.path, args.phase, args.argv)
    else:
        result = verify_cargo(args.output, args.sdk, json.loads(args.runtime), args.archive)
        print(result["cargo_invocation_sha256"], result["cargo_artifacts_sha256"])


if __name__ == "__main__":
    main()
