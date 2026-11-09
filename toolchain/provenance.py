"""Build-input records, not signed attestations or reproducible-build claims."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from bootstrap import RUST_NOTICES, load_pins, nodejs_hosts, rust_hosts
from file_metadata import STAMP, sha256, tree_sha256
from runtime_identity import selected_runtime

RESOURCE = "toolchain-metadata/BUILD.json"


def describe_tree(root: Path):
    digest = tree_sha256(root)
    record = {"tree_sha256": digest, "verification": "unverified-local-input"}
    path = root / STAMP
    if path.exists():
        bootstrap = json.loads(path.read_text())
        if bootstrap.get("schema_version") != 1 or bootstrap.get("tree_sha256") != digest:
            raise ValueError(f"Bootstrap tree changed or stamp is invalid: {root}")
        record.update(verification="bootstrap-record-matches-tree", bootstrap=bootstrap)
    return record


def package_metadata(repo, cosmocc, rust_compiler=None, rust_source=None, rust_sdk=None,
                     nodejs=None, webgpu=None, machin_source=None, machin_binary=None):
    runtime = selected_runtime(cosmocc)
    if rust_sdk is not None:
        if selected_runtime(rust_sdk / "vendor/cosmocc") != runtime:
            raise ValueError("Packaged Rust and C SDK runtime identities differ")
        if runtime["kind"] != "stock-sdk":
            sys.path.insert(0, str(repo / "third_party/rust_ape"))
            from runtime_profile import verify_binding
            binding = verify_binding(rust_sdk)
            if binding["runtime_identity"] != runtime["runtime_identity"]:
                raise ValueError("Packaged Rust binding differs from the selected C runtime")
    if webgpu is not None:
        webgpu_record = json.loads((webgpu / "manifest.json").read_text())
        if not isinstance(webgpu_record, dict):
            raise ValueError("WebGPU SDK manifest must be an object")
        if webgpu_record.get("runtime", {"kind": "stock-sdk"}) != runtime:
            raise ValueError("Packaged WebGPU SDK belongs to a different runtime")
        if runtime["kind"] != "stock-sdk":
            # Staging already verified the archive closure. Keep those exact
            # staged bytes instead of blessing a modified SDK under its old ID.
            files = webgpu_record.get("files")
            if not isinstance(files, dict) or "link.args" not in files:
                raise ValueError("Source WebGPU SDK requires its complete staged inventory")
            paths = list(webgpu.rglob("*"))
            if any(p.is_symlink() for p in paths):
                raise ValueError("Source WebGPU SDK cannot contain symlinks")
            actual = {p.relative_to(webgpu).as_posix(): sha256(p)
                      for p in paths if p.is_file() and p != webgpu / "manifest.json"}
            if actual != files:
                raise ValueError("Source WebGPU SDK changed after staging")
    inputs = {"cosmocc": describe_tree(cosmocc)}
    notices = {}
    extra_files = {}
    for key, path in (("rust_compiler", rust_compiler), ("rust_ape_source", rust_source),
                      ("rust_ape_sdk", rust_sdk), ("nodejs", nodejs),
                      ("webgpu", webgpu),
                      ("machin", machin_source)):
        if path is not None:
            inputs[key] = describe_tree(path)
    if rust_compiler:
        hosts = rust_hosts(rust_compiler)
        inputs["rust_compiler"]["hosts"] = hosts
        for host in hosts:
            for name in RUST_NOTICES:
                relative = "hosts/" + host + "/sysroot/" + name
                notices["rust-toolchain/" + relative] = sha256(rust_compiler / relative)
            sccache_license = rust_compiler / "hosts" / host / "sysroot/share/doc/sccache/LICENSE"
            if sccache_license.is_file():
                relative = "hosts/" + host + "/sysroot/share/doc/sccache/LICENSE"
                notices["rust-toolchain/" + relative] = sha256(sccache_license)
    if nodejs:
        hosts = nodejs_hosts(nodejs)
        inputs["nodejs"]["hosts"] = hosts
        for host in hosts:
            relative = "hosts/" + host + "/share/doc/nodejs/LICENSE"
            notices["nodejs/" + relative] = sha256(nodejs / relative)
    if webgpu:
        license_root = webgpu / "licenses"
        if not license_root.is_dir():
            raise ValueError("WebGPU SDK licenses are missing")
        for path in sorted(license_root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(webgpu).as_posix()
                notices["webgpu/" + relative] = sha256(path)
    if machin_source:
        license_path = machin_source / "LICENSE"
        archive_name = "toolchain-metadata/licenses/machin/LICENSE"
        notices[archive_name] = sha256(license_path)
        extra_files[archive_name] = license_path
        if machin_binary is None:
            raise ValueError("Machin provenance requires the bootstrap compiler")
        digest = sha256(machin_binary)
        inputs["machin"]["compiler_sha256"] = digest
        expected = inputs["machin"].get("bootstrap", {}).get("compiler_sha256")
        if expected is not None and expected != digest:
            raise ValueError("Machin bootstrap compiler changed")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                            text=True, stdout=subprocess.PIPE).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                           cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout
    return {
        "schema_version": 1,
        "runtime": runtime,
        "source_commit": commit,
        "source_worktree_dirty": bool(dirty.strip()),
        "requested_pins": load_pins(repo),
        "inputs": inputs,
        "notices": notices,
        "scope": "Input fingerprints and local bootstrap records, not signed attestations. "
                 "Unstamped inputs are unverified. Runner OS and system packages are not pinned. "
                 "Tree hashes describe inputs before packaging filters and generated config.",
    }, extra_files


def record_artifact(executable):
    with zipfile.ZipFile(executable) as archive:
        record = json.loads(archive.read(RESOURCE))
        for name, expected in record["notices"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError(f"Packaged notice differs: {name}")
    digest = sha256(executable)
    record["artifact"] = {"file": executable.name, "sha256": digest,
                          "size": executable.stat().st_size}
    (executable.parent / "BUILD.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    (executable.parent / "SHA256SUMS").write_text(f"{digest}  {executable.name}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    record_artifact(parser.parse_args().executable)
