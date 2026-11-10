#!/usr/bin/env python3
"""Produce a whole source runtime SDK, or verify its content identity.

Builds are explicit, expensive compile/link operations. No runtime tests are run.
The stock SDK is a compiler input, never the source of the final libc/CRT/headers.
Manifests are local content records, not authenticated build attestations.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import json

from bootstrap import load_pins
from file_metadata import STAMP, tree_sha256
from runtime_identity import (MANIFEST, SOURCE_INPUTS, ARCHITECTURES, digest_file,
                              inventory, read_json, require_layout,
                              require_source_path, runtime_identity,
                              source_inventory_sha256, verify_runtime_sdk)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def require_new(path):
    if path.exists() or path.is_symlink():
        raise ValueError(f"Refusing pre-existing output: {path}")


def tracked_source_inventory(root, revision, tree):
    """Fingerprint every committed input byte in the checkout actually built.

    Git's index must equal the pinned tree, and each working file must equal its
    indexed Git blob even if assume-unchanged/skip-worktree flags hide drift.
    Generated untracked/ignored outputs are not source inputs. Git modes are
    recorded rather than host umask-dependent permissions. Source link targets
    are hashed as bytes, never followed; the SDK's separate link policy is not
    relaxed by this record.
    """
    root = Path(root).resolve(strict=True)
    if (Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root or
            git(root, "rev-parse", "HEAD") != revision or
            git(root, "rev-parse", "HEAD^{tree}") != tree):
        raise ValueError("Source checkout no longer matches the pinned revision/tree")

    def git_entries(*args):
        raw = subprocess.run(["git", "-C", str(root), *args], check=True,
                             stdout=subprocess.PIPE).stdout
        if not raw or not raw.endswith(b"\0"):
            raise ValueError("Missing or malformed complete tracked source listing")
        entries = {}
        for item in raw[:-1].split(b"\0"):
            header, raw_name = item.split(b"\t", 1)
            first, second, third = header.decode("ascii").split(" ")
            name = os.fsdecode(raw_name)
            require_source_path(name)
            if name in entries:
                raise ValueError(f"Duplicate/unmerged tracked source: {name}")
            if first == "160000":
                raise ValueError("Submodules require explicit source identity support")
            if args[0] == "ls-files":
                if third != "0":
                    raise ValueError(f"Unmerged tracked source: {name}")
                mode, blob = first, second
            else:
                if second != "blob":
                    raise ValueError(f"Unsupported tracked source object: {name}")
                mode, blob = first, third
            if mode not in ("100644", "100755", "120000"):
                raise ValueError(f"Unsupported tracked source mode: {name}")
            entries[name] = (mode, blob)
        return entries

    index = git_entries("ls-files", "--stage", "-z")
    if index != git_entries("ls-tree", "-r", "-z", "--full-tree", revision):
        raise ValueError("Source index differs from the pinned source tree")
    files = {}
    for name, (mode, blob) in sorted(index.items()):
        path = root / name
        parent = path.parent
        while parent != root:
            if parent.is_symlink() or not parent.is_dir():
                raise ValueError(f"Tracked source has a non-directory ancestor: {name}")
            parent = parent.parent
        before = path.lstat()
        entry = {"mode": mode}
        sha256 = hashlib.sha256()
        git_hash = hashlib.new("sha1" if len(blob) == 40 else "sha256")
        if mode == "120000":
            if not stat.S_ISLNK(before.st_mode):
                raise ValueError(f"Tracked source symlink changed type: {name}")
            target = os.readlink(os.fsencode(path))
            git_hash.update(f"blob {len(target)}\0".encode() + target)
            sha256.update(target)
            entry.update(type="symlink", size=len(target), target=os.fsdecode(target))
        else:
            executable = bool(before.st_mode & stat.S_IXUSR)
            if not stat.S_ISREG(before.st_mode) or executable != (mode == "100755"):
                raise ValueError(f"Tracked source file changed type or mode: {name}")
            git_hash.update(f"blob {before.st_size}\0".encode())
            size = 0
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(block)
                    sha256.update(block)
                    git_hash.update(block)
            entry.update(type="file", size=size)
        after = path.lstat()
        attributes = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in attributes):
            raise ValueError(f"Tracked source changed while fingerprinting: {name}")
        if git_hash.hexdigest() != blob:
            raise ValueError(f"Tracked source bytes differ from the pinned Git blob: {name}")
        entry["sha256"] = sha256.hexdigest()
        files[name] = entry
    return files


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
        source_files = tracked_source_inventory(checkout, revision, tree)
        if load_pins(checkout)["cosmocc"] != pins:
            raise ValueError("Source bootstrap pins differ from the pinned checkout")
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
        if tracked_source_inventory(checkout, revision, tree) != source_files:
            raise ValueError("Tracked source inputs changed during build")
        require_layout(staged)
        source_identity = {"repository": repository, "revision": revision, "tree": tree, "clean": True}
        inputs_path = staged / SOURCE_INPUTS
        require_new(inputs_path)
        inputs_path.write_text(json.dumps({
            "schema_version": 1, "kind": "cosmopolitan-tracked-source-inputs",
            "source": source_identity, "files": source_files,
        }, indent=2, sort_keys=True) + "\n")
        record = {
            "schema_version": 1, "kind": "source-built-cosmopolitan-runtime",
            "runtime_abi": "cosmopolitan-source-runtime-v1",
            "architectures": ARCHITECTURES,
            "source": source_identity,
            "source_tree_sha256": source_inventory_sha256(source_files),
            "source_inventory_record": {"path": SOURCE_INPUTS, "sha256": digest_file(inputs_path)},
            "source_patches": [],
            "source_abi_inputs": features,
            "bootstrap": {"role": "stock-compiler-input-only", "version": pins["version"],
                          "archive_sha256": pins["sha256"], "tree_sha256": bootstrap_hash},
            "producer": {"entrypoint": "tool/cosmocc/package.sh",
                         "sha256": source_files["tool/cosmocc/package.sh"]["sha256"],
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
