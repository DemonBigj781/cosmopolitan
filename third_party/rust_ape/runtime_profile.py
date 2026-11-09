#!/usr/bin/env python3
"""Bind staged Rust shims to one verified complete source-built C/C++ SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "toolchain"))
from runtime_identity import verify_runtime_sdk
from stage import verify_shims

PROFILE = ".cosmo-runtime-profile.json"
BINDING = ".cosmo-runtime-binding.json"
TABLES = ("shim/tables.h", "examples/src/bin/shim_tables_check.rs")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, record: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", prefix=path.name + ".",
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def require_profile(sdk: Path) -> dict:
    profile = json.loads((sdk / PROFILE).read_text())
    if (profile.get("schema_version") != 1 or profile.get("profile") != "source-built" or
            profile.get("runtime_abi") != "cosmopolitan-source-runtime-v1"):
        raise ValueError("Rust source-runtime profile was not staged in this SDK")
    verify_shims(sdk, profile)
    return profile


def activate(sdk: Path, runtime: Path) -> str:
    sdk = sdk.resolve(strict=True)
    profile = require_profile(sdk)
    runtime = runtime.resolve(strict=True)
    record = verify_runtime_sdk(runtime)
    if (record.get("runtime_abi") != profile["runtime_abi"] or
            record.get("source_abi_inputs") != profile["source_abi_inputs"]):
        raise ValueError("Source SDK ABI does not match the staged Rust shim profile")
    destination = sdk / "vendor/cosmocc"
    binding = sdk / BINDING
    expected = {"schema_version": 1, "runtime_identity": record["runtime_identity"],
                "runtime_abi": profile["runtime_abi"], "profile": profile}
    if binding.exists():
        previous = json.loads(binding.read_text())
        if any(previous.get(key) != value for key, value in expected.items()):
            raise ValueError("Rust runtime binding changed; choose a new COSMO_RUST_BUILD_ROOT")
        installed = verify_runtime_sdk(destination)
        if installed["runtime_identity"] != record["runtime_identity"]:
            raise ValueError("Staged Rust SDK contains a different C/C++ runtime")
        return record["runtime_identity"]
    if (destination / "SOURCE_RUNTIME.json").exists():
        raise ValueError("Unrecorded source SDK installation; choose a new Rust build root")
    if any((sdk / "generated").glob("shim-*.o")):
        raise ValueError("Rust shim objects already exist before runtime binding; use a fresh build root")
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("Expected the owned bootstrap SDK directory before source-runtime binding")
    if runtime == destination or destination in runtime.parents or runtime in destination.parents:
        raise ValueError("Source SDK and Rust SDK cannot overlap")
    # Never partially overlay a runtime: preserve complete relative aliases,
    # verify the copy, then replace the bootstrap tree as one directory.
    with tempfile.TemporaryDirectory(prefix="source-runtime-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "sdk"
        shutil.copytree(runtime, staged, symlinks=True)
        copied = verify_runtime_sdk(staged)
        if copied["runtime_identity"] != record["runtime_identity"]:
            raise ValueError("Source SDK changed while it was copied")
        old = Path(temporary) / "bootstrap"
        destination.rename(old)
        try:
            staged.rename(destination)
            atomic_json(binding, expected)
        except BaseException:
            # Publication is part of the transaction, not just the tree swap.
            if destination.exists():
                destination.rename(staged)
            old.rename(destination)
            raise
    return record["runtime_identity"]


def record_tables(sdk: Path) -> None:
    require_profile(sdk)
    record = json.loads((sdk / BINDING).read_text())
    installed = verify_runtime_sdk(sdk / "vendor/cosmocc")
    if installed["runtime_identity"] != record.get("runtime_identity"):
        raise ValueError("Runtime changed before ABI table generation completed")
    record["generated_tables"] = {path: sha(sdk / path) for path in TABLES}
    atomic_json(sdk / BINDING, record)


def verify_binding(sdk: Path) -> dict:
    profile = require_profile(sdk)
    record = json.loads((sdk / BINDING).read_text())
    installed = verify_runtime_sdk(sdk / "vendor/cosmocc")
    if (record.get("profile") != profile or
            installed["runtime_identity"] != record.get("runtime_identity") or
            record.get("generated_tables") != {path: sha(sdk / path) for path in TABLES}):
        raise ValueError("Rust SDK runtime binding or generated ABI tables changed")
    return record


def expected_shim_objects(sdk: Path, arch: str = "x86_64", tiny: bool = False) -> list[Path]:
    profile = require_profile(sdk)
    if arch not in ("x86_64", "aarch64"):
        raise ValueError("Unknown Rust shim architecture")
    suffix = f"-{arch}" + ("-tiny" if tiny else "") + ".o"
    names = ["shim-" + name.removeprefix("shim/").removesuffix(".c").replace("/", "-") + suffix
             for name in profile["shim_sources_sha256"] if name.startswith("shim/") and name.endswith(".c")]
    if not names or len(names) != len(set(names)):
        raise ValueError("Rust shim object names are empty or collide")
    actual = {path.name for path in (sdk / "generated").glob("shim-*" + suffix)}
    if actual != set(names):
        raise ValueError("Rust shim object set differs from the verified source profile")
    paths = [sdk / "generated" / name for name in sorted(names)]
    if any(path.is_symlink() or not path.is_file() or path.stat().st_size == 0 for path in paths):
        raise ValueError("Rust shim object is missing, empty, or a symlink")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("activate", "record-tables", "verify"))
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    if args.operation == "activate":
        if args.runtime is None:
            parser.error("activate requires --runtime")
        print(activate(args.sdk, args.runtime))
    elif args.operation == "record-tables":
        record_tables(args.sdk)
    else:
        print(json.dumps(verify_binding(args.sdk), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"Rust source-runtime binding failed: {error}") from error
