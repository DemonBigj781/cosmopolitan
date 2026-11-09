#!/usr/bin/env python3
"""Materialize the pinned SDK once without resetting its working lockfiles."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
PROFILE_MARKER = ".cosmo-runtime-profile.json"


def stage(source: Path, sdk: Path, profile: str = "stock-sdk") -> None:
    source = source.resolve(strict=True)
    sdk = sdk.resolve()
    if profile not in ("stock-sdk", "source-built"):
        raise ValueError(f"Unknown runtime profile: {profile}")
    if profile == "source-built":
        pin = json.loads((HERE / "source-runtime.json").read_text())
        patch = HERE / pin["patch"]["path"]
        if hashlib.sha256(patch.read_bytes()).hexdigest() != pin["patch"]["sha256"]:
            raise ValueError("Source-runtime shim patch does not match its pin")
        with tempfile.TemporaryDirectory(prefix="rust-source-profile-") as temporary:
            transformed = Path(temporary) / "source"
            for path in source.rglob("*"):
                if path.is_symlink():
                    raise ValueError(f"Unexpected SDK source symlink: {path}")
            shutil.copytree(source, transformed)
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                           cwd=transformed, check=True, stdout=subprocess.PIPE)
            _stage(transformed, sdk, profile, pin)
    else:
        _stage(source, sdk, profile, None)


def _stage(source: Path, sdk: Path, profile: str, pin: dict | None) -> None:
    files = []
    fingerprint = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Unexpected symlink in the pinned source snapshot: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        content = path.read_bytes()
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        fingerprint.update(relative.as_posix().encode() + b"\0")
        fingerprint.update(bytes([executable]))
        fingerprint.update(hashlib.sha256(content).digest())
        files.append((path, relative, content, executable))

    stamp = sdk / ".cosmo-source-stamp"
    if pin:
        fingerprint.update(json.dumps(pin, sort_keys=True).encode())
    expected = fingerprint.hexdigest() + "\n"
    marker = sdk / PROFILE_MARKER
    metadata = {"schema_version": 1, "profile": profile}
    if pin:
        metadata.update(runtime_abi=pin["runtime_abi"],
                        integration_revision=pin["integration_revision"],
                        patch_sha256=pin["patch"]["sha256"],
                        source_abi_inputs=pin["source_abi_inputs"],
                        shim_sources_sha256={relative.as_posix(): hashlib.sha256(content).hexdigest()
                            for _, relative, content, _ in files
                            if (relative.parts[0] == "shim" and relative.as_posix() != "shim/tables.h")
                            or relative.as_posix() == "scripts/gcc-linker-wrapper.bash"})
    if marker.exists() and json.loads(marker.read_text()) != metadata:
        raise RuntimeError("The runtime profile changed; choose a new COSMO_RUST_BUILD_ROOT")
    if stamp.exists():
        if stamp.read_text() != expected:
            raise RuntimeError(
                "The vendored SDK changed. Choose a new COSMO_RUST_BUILD_ROOT; "
                "existing SDK sources and lockfiles were left unchanged."
            )
        if pin:
            if not marker.is_file():
                raise RuntimeError("Source runtime SDK profile marker is missing")
            verify_shims(sdk, metadata)
        return

    # An old or partially materialized SDK can be adopted only when all source
    # files already present match the pinned snapshot. Never silently reset it.
    for path, relative, content, executable in files:
        destination = sdk / relative
        destination.parent.resolve().relative_to(sdk)
        if destination.is_symlink():
            raise RuntimeError(f"Unexpected symlink in staged SDK source: {destination}")
        if destination.exists():
            if (not destination.is_file() or destination.read_bytes() != content or
                    bool(destination.stat().st_mode & stat.S_IXUSR) != executable):
                raise RuntimeError(f"Staged SDK source differs; refusing to reset {destination}")

    for path, relative, _, _ in files:
        destination = sdk / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    if pin:
        verify_shims(sdk, metadata)
    stamp.write_text(expected)
    marker.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def verify_shims(sdk: Path, metadata: dict) -> None:
    expected = metadata["shim_sources_sha256"]
    actual = {path.relative_to(sdk).as_posix() for path in (sdk / "shim").rglob("*")
              if path.is_file() and path.relative_to(sdk).as_posix() != "shim/tables.h"}
    if actual != {name for name in expected if name.startswith("shim/")}:
        raise RuntimeError("Staged source-runtime shim file set changed; use a fresh build root")
    for name, digest in expected.items():
        path = sdk / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != sdk and sdk in parent.parents):
            raise RuntimeError(f"Source-runtime shim cannot be a symlink: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Staged source-runtime adapter changed: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("sdk", type=Path)
    parser.add_argument("--runtime-profile", choices=("stock-sdk", "source-built"), default="stock-sdk")
    args = parser.parse_args()
    stage(args.source, args.sdk, args.runtime_profile)


if __name__ == "__main__":
    main()
