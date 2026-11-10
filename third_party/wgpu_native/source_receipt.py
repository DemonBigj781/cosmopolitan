#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Freeze explicit application patches and verify prepared WebGPU source receipts."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

FOUNDATION_INPUTS = (
    "IMPORT.json", "Cargo.cosmo.lock", "prepare.py", "source_receipt.py",
    "generate_vulkan_signatures.py", "patches/wgpu-native-cosmopolitan.patch",
    "patches/ash-cosmopolitan.patch", "patches/wgpu-hal-cosmopolitan.patch",
)
PATCHED_CRATES = {
    "ash": ("0.38.0+1.3.281", "0bb44936d800fea8f016d7f2311c6a4f97aebd5dc86f09906139ec848cf3a46f"),
    "wgpu-hal": ("29.0.3", "31f8e1a9e7a8512f276f7c62e018c7fa8d60954303fed2e5750114332049193f"),
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def encoded(value):
    """Canonical UTF-8 JSON for receipt tree hashes, without a trailing newline."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalized_relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or
            any(ord(char) < 32 for char in value) or
            any(part in ("", ".", "..") for part in value.split("/")) or
            PurePosixPath(value).is_absolute()):
        raise ValueError(f"Expected a normalized relative path: {value!r}")
    return value


def relative_name(value):
    normalized_relative_path(value)
    if not re.fullmatch(r"[A-Za-z0-9_.+/-]+", value):
        raise ValueError(f"Expected a normalized relative patch name: {value!r}")
    return value


@contextmanager
def regular_reader(path):
    """Reject symlinks in the entire path and special files before reading."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError(f"Symlink input is not permitted: {component}")
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"Input changed while opening: {path}")
        yield stream
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (
                current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"Input changed while reading: {path}")


def regular_bytes(path):
    with regular_reader(path) as stream:
        return stream.read()


def file_sha256(path):
    with regular_reader(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


@contextmanager
def frozen_application_patches(manifest_path=None):
    """Validate every input before yielding owned, checksum-verified patch files.

    Relative patch paths are relative to the manifest, never the build cwd.
    The manifest and each original patch are read once. Application code uses
    only our private copies, even if the caller changes an input afterward.
    """
    manifest_data = None
    entries = []
    if manifest_path:
        manifest_path = Path(manifest_path)
        if not manifest_path.is_absolute():
            manifest_path = Path.cwd() / manifest_path
        manifest_data = regular_bytes(manifest_path)
        manifest = parse_json(manifest_data)
        if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "patches"}
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or not isinstance(manifest["patches"], list)):
            raise ValueError("Application patch manifest requires schema_version 1 and patches list")
        names = set()
        for entry in manifest["patches"]:
            if not isinstance(entry, dict) or set(entry) != {"name", "path", "sha256"}:
                raise ValueError("Every application patch requires exactly name, path and sha256")
            name = relative_name(entry["name"])
            if name in names or any(name.startswith(other + "/") or other.startswith(name + "/")
                                    for other in names):
                raise ValueError(f"Duplicate or overlapping application patch name: {name}")
            names.add(name)
            if not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"]):
                raise ValueError(f"Invalid application patch SHA-256: {name}")
            value = entry["path"]
            if not isinstance(value, str) or not value or "\0" in value:
                raise ValueError(f"Invalid application patch path: {name}")
            path = Path(value)
            if not path.is_absolute():
                path = manifest_path.parent / path
            data = regular_bytes(path)
            if sha256(data) != entry["sha256"]:
                raise ValueError(f"Application patch checksum mismatch: {name}")
            entries.append((name, data, entry["sha256"]))
    with tempfile.TemporaryDirectory(prefix="wgpu-application-patches-") as directory:
        root = Path(directory)
        for name, data, checksum in entries:
            path = root / "patches" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o444)
            if file_sha256(path) != checksum:
                raise ValueError(f"Owned patch copy changed: {name}")
        provenance = {
            "schema_version": 1,
            "manifest_sha256": sha256(manifest_data) if manifest_data is not None else None,
            "patches": [{"name": name, "path": "patches/" + name, "sha256": checksum}
                        for name, _, checksum in entries],
        }
        (root / "MANIFEST.json").write_bytes(encoded(provenance) + b"\n")
        if manifest_data is not None:
            (root / "INPUT_MANIFEST.json").write_bytes(manifest_data)
        yield root, provenance


def tree_receipt(root):
    """Hash all file bytes, names and permission bits; reject links/special files."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Expected an ordinary source directory: {root}")
    entries = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            entries[relative] = {"kind": "file", "mode": mode, "sha256": file_sha256(path)}
        elif stat.S_ISDIR(info.st_mode):
            entries[relative] = {"kind": "directory", "mode": mode}
        else:
            raise ValueError(f"Non-regular source entry: {path}")
    return {"sha256": sha256(encoded(entries)), "entries": entries}


def verify_pristine(root, manifest):
    expected = {}
    projects = manifest["sources"]
    if {project["project"] for project in projects} != {"wgpu-native", "webgpu-headers"} or len(projects) != 2:
        raise ValueError("Unexpected pristine WebGPU import projects")
    for project in projects:
        prefix = "ffi/webgpu-headers/" if project["project"] == "webgpu-headers" else ""
        for entry in project["included"]:
            name = prefix + normalized_relative_path(entry["path"])
            if name in expected:
                raise ValueError(f"Duplicate pristine import path: {name}")
            expected[name] = entry["sha256"]
    actual = {name: entry["sha256"] for name, entry in tree_receipt(root)["entries"].items()
              if entry["kind"] == "file"}
    if actual != expected:
        changed = sorted(name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name))
        raise ValueError("Modified or unlisted pristine import files: " + ", ".join(changed))


def crate_pins():
    return {name: {"version": version, "archive_sha256": checksum}
            for name, (version, checksum) in PATCHED_CRATES.items()}


def source_roots():
    return {"source", *(f"deps/{name}-{version}" for name, (version, _) in PATCHED_CRATES.items())}


def foundation_inputs(root, *, allow_missing_lock=False):
    return {name: (None if allow_missing_lock and name == "Cargo.cosmo.lock" and
                   not (root / name).exists() and not (root / name).is_symlink()
                   else file_sha256(root / name)) for name in FOUNDATION_INPUTS}


def write_prepared(output, foundation, crate_paths, provenance, *, bootstrap_lock=False,
                   expected_foundation=None):
    inputs = foundation_inputs(foundation, allow_missing_lock=bootstrap_lock)
    if expected_foundation is not None and inputs != expected_foundation:
        raise ValueError("Foundation preparation inputs changed while preparing sources")
    roots = {"source", *(path.relative_to(output).as_posix() for path in crate_paths.values())}
    if roots != source_roots() or set(crate_paths) != set(PATCHED_CRATES):
        raise ValueError("Prepared dependency roots do not match pinned crates")
    trees = {name: tree_receipt(output / name) for name in sorted(roots)}
    receipt = {
        "schema_version": 1,
        "application_patches": {entry["name"]: entry["sha256"] for entry in provenance["patches"]},
        "application_patch_order": [entry["name"] for entry in provenance["patches"]],
        "application_patch_manifest_sha256": provenance["manifest_sha256"],
        "application_patch_provenance_path": "application-patches",
        "application_patch_provenance": tree_receipt(output / "application-patches"),
        "foundation_inputs": inputs,
        "foundation_pins": parse_json(regular_bytes(foundation / "IMPORT.json")),
        "crate_pins": crate_pins(),
        "bootstrap_lock": bootstrap_lock,
        "prepared_lock_sha256": file_sha256(output / "source/Cargo.lock"),
        "source_trees": trees,
        "prepared_source_sha256": sha256(encoded(trees)),
        "generated_inputs": {name: file_sha256(output / name) for name in (
            "cargo.cosmo.toml", "generated/vulkan_signatures.inc")},
    }
    (output / "PREPARED.json").write_bytes(encoded(receipt) + b"\n")
    return receipt


def verify_prepared(output, foundation, expected_sha256=None, *, allow_bootstrap=False):
    raw = regular_bytes(output / "PREPARED.json")
    checksum = sha256(raw)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ValueError("WebGPU preparation receipt changed during the build")
    receipt = parse_json(raw)
    if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise ValueError("Unsupported WebGPU preparation receipt")
    if receipt.get("bootstrap_lock") is not False and not allow_bootstrap:
        raise ValueError("Bootstrap-lock preparation cannot authorize a pinned build")
    if receipt["foundation_inputs"] != foundation_inputs(
            foundation, allow_missing_lock=receipt.get("bootstrap_lock") is True):
        raise ValueError("Foundation preparation inputs changed")
    if receipt["foundation_pins"] != parse_json(regular_bytes(foundation / "IMPORT.json")):
        raise ValueError("Foundation pins differ from the prepared receipt")
    trees = receipt["source_trees"]
    if set(trees) != source_roots() or receipt.get("crate_pins") != crate_pins():
        raise ValueError("Prepared source trees do not match pinned crates")
    for name, expected in trees.items():
        relative_name(name)
        if name != "source" and not name.startswith("deps/"):
            raise ValueError("Unexpected prepared source root")
        if tree_receipt(output / name) != expected:
            raise ValueError(f"Prepared WebGPU source changed: {name}")
    if sha256(encoded(trees)) != receipt["prepared_source_sha256"]:
        raise ValueError("Invalid prepared source digest")
    if file_sha256(output / "source/Cargo.lock") != receipt["prepared_lock_sha256"]:
        raise ValueError("Prepared lockfile changed")
    if set(receipt["generated_inputs"]) != {"cargo.cosmo.toml", "generated/vulkan_signatures.inc"}:
        raise ValueError("Unexpected generated preparation inputs")
    for name, expected in receipt["generated_inputs"].items():
        if file_sha256(output / name) != expected:
            raise ValueError(f"Generated WebGPU input changed: {name}")
    if receipt["application_patch_provenance_path"] != "application-patches":
        raise ValueError("Unexpected application patch provenance path")
    root = output / "application-patches"
    if tree_receipt(root) != receipt["application_patch_provenance"]:
        raise ValueError("Application patch provenance changed")
    provenance = parse_json(regular_bytes(root / "MANIFEST.json"))
    patches = provenance["patches"]
    if ([entry["name"] for entry in patches] != receipt["application_patch_order"] or
            {entry["name"]: entry["sha256"] for entry in patches} != receipt["application_patches"] or
            provenance["manifest_sha256"] != receipt["application_patch_manifest_sha256"]):
        raise ValueError("Application patch provenance differs from preparation")
    for entry in patches:
        name = relative_name(entry["name"])
        if entry["path"] != "patches/" + name or file_sha256(root / entry["path"]) != entry["sha256"]:
            raise ValueError(f"Application patch provenance bytes changed: {name}")
    if provenance["manifest_sha256"] is not None:
        if file_sha256(root / "INPUT_MANIFEST.json") != provenance["manifest_sha256"]:
            raise ValueError("Application patch input manifest changed")
    return receipt, checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--foundation", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    _, checksum = verify_prepared(args.output, args.foundation, args.expected_sha256)
    print(checksum)


if __name__ == "__main__":
    main()
