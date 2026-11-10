#!/usr/bin/env python3
"""Materialize verified upstream sources and reviewed patches under o/webgpu."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from source_receipt import (PATCHED_CRATES as CRATES, foundation_inputs,
                            frozen_application_patches, regular_bytes, sha256,
                            verify_pristine, write_prepared)

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def apply(path, patch):
    subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i",
                    str(patch)], cwd=path, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-lock", action="store_true",
                        help="maintainer operation: start from upstream Cargo.lock")
    parser.add_argument("--application-patch-manifest",
                        default=os.environ.get("WGPU_APPLICATION_PATCH_MANIFEST"),
                        help="explicit application patch JSON (or WGPU_APPLICATION_PATCH_MANIFEST)")
    args = parser.parse_args()
    # Validate ALL caller bytes before deleting or materializing any source.
    with frozen_application_patches(args.application_patch_manifest) as (patches, provenance):
        prepare(args, patches, provenance)


def prepare(args, application_root, provenance):
    initial_foundation = foundation_inputs(ROOT, allow_missing_lock=args.bootstrap_lock)
    # Apply our own immutable copies too; verify the reviewed inputs at the end.
    with tempfile.TemporaryDirectory(prefix="wgpu-foundation-patches-") as directory:
        port_patches = Path(directory)
        for name in initial_foundation:
            if name.startswith("patches/"):
                data = regular_bytes(ROOT / name)
                if sha256(data) != initial_foundation[name]:
                    raise ValueError(f"Foundation patch changed before preparation: {name}")
                (port_patches / Path(name).name).write_bytes(data)
        materialize(args, application_root, provenance, initial_foundation, port_patches)


def materialize(args, application_root, provenance, initial_foundation, port_patches):
    output = REPO / "o/webgpu"
    sources = output / "source"
    deps = output / "deps"
    deps.mkdir(parents=True, exist_ok=True)
    # Failed preparation must never leave a valid-looking receipt from an older build.
    (output / "PREPARED.json").unlink(missing_ok=True)
    manifest = json.loads((ROOT / "IMPORT.json").read_text())
    verify_pristine(ROOT / "upstream", manifest)
    # These are disposable materializations; originals stay in third_party.
    if sources.exists():
        shutil.rmtree(sources)
    shutil.copytree(ROOT / "upstream", sources)
    verify_pristine(sources, manifest)
    apply(sources, port_patches / "wgpu-native-cosmopolitan.patch")
    paths = {}
    for name, (version, checksum) in CRATES.items():
        archive = deps / f"{name}.crate"
        if not archive.exists():
            url = f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
            temporary = archive.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as sink:
                shutil.copyfileobj(response, sink)
            temporary.replace(archive)
        if digest(archive) != checksum:
            raise SystemExit(f"Checksum mismatch: {archive}; remove the corrupt cached download")
        path = deps / f"{name}-{version}"
        if path.exists():
            shutil.rmtree(path)
        with tarfile.open(archive) as source:
            # Python's data filter rejects device nodes, escaping paths and links.
            source.extractall(deps, filter="data")
        apply(path, port_patches / f"{name}-cosmopolitan.patch")
        paths[name] = path
    config = "[patch.crates-io]\n"
    for name, path in paths.items():
        config += f"{name} = {{ path = {json.dumps(str(path))} }}\n"
    (output / "cargo.cosmo.toml").write_text(config)
    lock = ROOT / "Cargo.cosmo.lock"
    if not args.bootstrap_lock:
        if not lock.is_file():
            raise SystemExit("Missing reviewed Cargo.cosmo.lock; do not resolve an unpinned build")
        shutil.copyfile(lock, sources / "Cargo.lock")
    # Application patches target the native source root, after all foundation
    # patches and the reviewed lockfile have been installed.
    for entry in provenance["patches"]:
        apply(sources, application_root / entry["path"])
    patch_destination = output / "application-patches"
    if patch_destination.exists():
        shutil.rmtree(patch_destination)
    shutil.copytree(application_root, patch_destination)
    subprocess.run(["python3", str(ROOT / "generate_vulkan_signatures.py"),
                    str(paths["ash"]), str(output / "generated/vulkan_signatures.inc")],
                   check=True)
    write_prepared(output, ROOT, paths, provenance, bootstrap_lock=args.bootstrap_lock,
                   expected_foundation=initial_foundation)


if __name__ == "__main__":
    main()
