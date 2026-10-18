"""Build-input records, not signed attestations or reproducible-build claims."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from bootstrap import RUST_NOTICES, load_pins, rust_hosts
from file_metadata import STAMP, sha256, tree_sha256

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
                     machin_source=None, machin_binary=None):
    inputs = {"cosmocc": describe_tree(cosmocc)}
    notices = {}
    extra_files = {}
    for key, path in (("rust_compiler", rust_compiler), ("rust_ape_source", rust_source),
                      ("rust_ape_sdk", rust_sdk), ("machin", machin_source)):
        if path is not None:
            inputs[key] = describe_tree(path)
    if rust_compiler:
        hosts = rust_hosts(rust_compiler)
        inputs["rust_compiler"]["hosts"] = hosts
        for host in hosts:
            for name in RUST_NOTICES:
                relative = "hosts/" + host + "/sysroot/" + name
                notices["rust-toolchain/" + relative] = sha256(rust_compiler / relative)
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
