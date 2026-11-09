#!/usr/bin/env python3
"""Produce a whole source runtime SDK, or verify its content identity.

Builds are explicit, expensive compile/link operations. No runtime tests are run.
The stock SDK is a compiler input, never the source of the final libc/CRT/headers.
Manifests are local content records, not authenticated build attestations.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import json

from bootstrap import load_pins
from file_metadata import STAMP, tree_sha256
from runtime_identity import (MANIFEST, ARCHITECTURES, canonical, digest_file,
                              inventory, read_json, require_layout,
                              runtime_identity, verify_runtime_sdk)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def require_new(path):
    if path.exists() or path.is_symlink():
        raise ValueError(f"Refusing pre-existing output: {path}")


def source_features(root):
    """Record source ABI inputs; consumers must still select matching adapters."""
    paths = ("libc/nt/struct/sid.h", "libc/dlopen/dladdr.c",
             "libc/intrin/fds.h", "libc/proc/posix_spawn.c")
    result = {}
    for name in paths:
        path = root / name
        if not path.is_file():
            raise ValueError(f"Missing required source ABI input: {name}")
        result[name] = digest_file(path)
    sid = (root / paths[0]).read_text()
    if sid.count("struct NtSidIdentifierAuthority {") != 1:
        raise ValueError("Source must have exactly one NtSidIdentifierAuthority definition")
    fd = (root / paths[2]).read_text()
    if not all(marker in fd for marker in ("atomic_char kind;", "bool was_created_during_vfork;", "unsigned flags;", "long handle;")):
        raise ValueError("Source file-descriptor ABI is not supported")
    dladdr = (root / paths[1]).read_text()
    if "dladdr(" not in dladdr:
        raise ValueError("Source must provide the independent dladdr implementation")
    pin = read_json(root / "third_party/rust_ape/source-runtime.json")
    if (pin.get("runtime_abi") != "cosmopolitan-source-runtime-v1" or
            pin.get("source_abi_inputs") != result):
        raise ValueError("Source ABI fingerprints differ from the reviewed Rust adapter profile")
    return result


def build_runtime_sdk(source, bootstrap_sdk, output, jobs=1):
    if type(jobs) is not int or jobs < 1:
        raise ValueError("jobs must be a positive integer")
    source = Path(source).resolve(strict=True)
    bootstrap_sdk = Path(bootstrap_sdk).absolute()
    output = Path(output).absolute()
    require_new(output)
    if git(source, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Source repository must be clean and committed")
    revision = git(source, "rev-parse", "HEAD")
    tree = git(source, "rev-parse", "HEAD^{tree}")
    repository = git(source, "remote", "get-url", "origin")
    if not repository:
        raise ValueError("Source repository needs an origin identity")
    if any(line.startswith("160000 ") for line in git(source, "ls-files", "--stage").splitlines()):
        raise ValueError("Submodules require explicit source identity support")
    pins = load_pins(source)["cosmocc"]
    # Inventory validates links even though the existing bootstrap tree hash
    # deliberately fingerprints link text rather than resolving it.
    inventory(bootstrap_sdk)
    stamp = bootstrap_sdk / STAMP
    if stamp.is_symlink() or not stamp.is_file():
        raise ValueError("Bootstrap compiler must have a verified bootstrap record")
    metadata = read_json(stamp)
    bootstrap_hash = tree_sha256(bootstrap_sdk)
    if (metadata.get("schema_version") != 1 or metadata.get("cosmocc") != pins or
            metadata.get("verification") != "archive-sha256-checked" or
            metadata.get("tree_sha256") != bootstrap_hash or
            (bootstrap_sdk / MANIFEST).exists()):
        raise ValueError("Stock bootstrap compiler identity does not match source pins")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="source-runtime-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        checkout = temporary / "source"
        # No worktree files, ignored object caches, or local hardlinks survive.
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet",
                        "--no-local", "--no-checkout", str(source), str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null",
                        "checkout", "--quiet", "--detach", revision], check=True)
        for name in ("o", ".cosmocc", "cosmocc"):
            require_new(checkout / name)
        features = source_features(checkout)
        bootstrap_copy = checkout / ".cosmocc" / pins["version"]
        shutil.copytree(bootstrap_sdk, bootstrap_copy, symlinks=True)
        if tree_sha256(bootstrap_copy) != bootstrap_hash:
            raise ValueError("Bootstrap compiler changed while copying")
        (checkout / ".cosmocc/current").symlink_to(pins["version"], target_is_directory=True)
        staged = temporary / "sdk"
        require_new(staged)
        # Never inherit MAKEFLAGS, CC/CXX, COSMOCC, CPATH, LIBRARY_PATH or
        # runtime profile overrides. System build utilities remain host inputs.
        env = {key: os.environ[key] for key in ("HOME", "PATH", "SYSTEMROOT") if key in os.environ}
        env.update(COSMO_PACKAGE_JOBS=str(jobs), LC_ALL="C", TMPDIR=str(temporary))
        subprocess.run(["sh", "tool/cosmocc/package.sh", str(staged)],
                       cwd=checkout, env=env, check=True)
        if tree_sha256(bootstrap_copy) != bootstrap_hash:
            raise ValueError("Bootstrap compiler mutated during build")
        require_layout(staged)
        record = {
            "schema_version": 1, "kind": "source-built-cosmopolitan-runtime",
            "runtime_abi": "cosmopolitan-source-runtime-v1",
            "architectures": ARCHITECTURES,
            "source": {"repository": repository, "revision": revision, "tree": tree, "clean": True},
            "source_abi_inputs": features,
            "bootstrap": {"role": "stock-compiler-input-only", "version": pins["version"],
                          "archive_sha256": pins["sha256"], "tree_sha256": bootstrap_hash},
            "producer": {"entrypoint": "tool/cosmocc/package.sh",
                         "sha256": digest_file(checkout / "tool/cosmocc/package.sh"),
                         "jobs": jobs, "fresh_outputs": True,
                         "compiler_distribution": "package.sh checksum-pinned GCC and LLVM archives"},
            "scope": "Local input/content identity, not signed provenance or reproducible-build proof. "
                     "Host build tools are not pinned. All runtime variants come from the same source tree.",
            "files": inventory(staged),
        }
        record["runtime_identity"] = runtime_identity(record)
        (staged / MANIFEST).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        verify_runtime_sdk(staged)
        require_new(output)
        staged.rename(output)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--sdk", type=Path, required=True)
    build = commands.add_parser("build")
    build.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--bootstrap-sdk", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    try:
        record = (verify_runtime_sdk(args.sdk) if args.command == "verify" else
                  build_runtime_sdk(args.source, args.bootstrap_sdk, args.output, args.jobs))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"source runtime: {error}\n")
    print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
