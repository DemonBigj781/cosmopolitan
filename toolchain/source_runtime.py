#!/usr/bin/env python3
"""Produce a whole source runtime SDK, or verify its content identity.

Builds are explicit, expensive compile/link operations. No runtime tests are run.
The stock SDK is a compiler input, never the source of the final libc/CRT/headers.
Manifests are local content records, not authenticated build attestations.
"""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import json

from bootstrap import load_pins
from file_metadata import STAMP, tree_sha256
from runtime_identity import (MANIFEST, SOURCE_INPUTS, ARCHITECTURES, digest_file,
                              inventory, read_json, require_layout,
                              require_source_path, runtime_identity,
                              source_inventory_sha256, verify_runtime_sdk)


def git_environment():
    # Pin operations to the selected repository, not an inherited worktree,
    # index, replacement object, external filter or process-global Git config.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_ATTR_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1")
    return env


def git_command(root, *args):
    return ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", *args]


def git_bytes(root, *args):
    return subprocess.run(git_command(root, *args), check=True, env=git_environment(),
                          stdout=subprocess.PIPE).stdout


def git(root, *args):
    return os.fsdecode(git_bytes(root, *args)).strip()


def require_new(path):
    if path.exists() or path.is_symlink():
        raise ValueError(f"Refusing pre-existing output: {path}")


def publish_new_directory(source, destination):
    """Atomically publish without replacing even a concurrent empty directory."""
    if not sys.platform.startswith("linux"):
        raise ValueError("Atomic no-replacement publication currently requires a Linux host")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise ValueError("Host libc lacks atomic no-replacement renameat2") from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def tracked_git_entries(root, *args):
    raw = git_bytes(root, *args)
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


def materialize_source(source, output, revision=None):
    """Create a new, isolated checkout from raw committed Git blob bytes.

    This explicit operation selects committed content, not the caller's working
    files or index. It never changes the caller's checkout. No checkout, status,
    clean/smudge filter, archive export transform or external filter is used.
    Use the resulting root for the SDK, dependency builds and final export.
    """
    source = Path(source).resolve(strict=True)
    output = Path(output).absolute()
    require_new(output)
    if output.resolve().is_relative_to(source):
        raise ValueError("Materialized source must be outside the input repository")
    revision = git(source, "rev-parse", "HEAD") if revision is None else revision
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("Source materialization requires a full committed revision")
    if git(source, "rev-parse", "--verify", revision + "^{commit}") != revision:
        raise ValueError("Source revision must identify a commit directly")
    tree = git(source, "rev-parse", revision + "^{tree}")
    repository = git(source, "remote", "get-url", "origin")
    if not repository:
        raise ValueError("Source repository needs an origin identity")
    entries = tracked_git_entries(source, "ls-tree", "-r", "-z", "--full-tree", revision)
    # Validate the whole path set before creating any symlinks or directories.
    for name in entries:
        parts = name.split("/")
        if any("/".join(parts[:i]) in entries for i in range(1, len(parts))):
            raise ValueError(f"Tracked source paths overlap: {name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.resolve() != output.parent:
        raise ValueError("Materialized source parent must contain no directory aliases")
    with tempfile.TemporaryDirectory(prefix="raw-source-", dir=output.parent) as temporary:
        checkout = Path(temporary) / "source"
        git_bytes(source, "-c", "core.autocrlf=false", "clone", "--quiet", "--no-local",
                  "--no-checkout", "--template=", "--", str(source), str(checkout))
        git(checkout, "config", "remote.origin.url", repository)
        git(checkout, "config", "core.autocrlf", "false")
        git(checkout, "update-ref", "--no-deref", "HEAD", revision)
        git(checkout, "read-tree", revision)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(git_command(checkout, "cat-file", "--batch"),
                                       env=git_environment(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=errors)
            try:
                for name, (mode, blob) in sorted(entries.items()):
                    process.stdin.write(blob.encode("ascii") + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if (len(header) != 3 or header[:2] != [blob.encode("ascii"), b"blob"] or
                            not header[2].isdigit()):
                        raise ValueError(f"Cannot read raw committed source blob: {name}")
                    remaining = int(header[2])
                    path = checkout / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if mode == "120000":
                        target = process.stdout.read(remaining)
                        if len(target) != remaining:
                            raise ValueError(f"Truncated source symlink blob: {name}")
                        os.symlink(target, os.fsencode(path))
                    else:
                        with path.open("xb") as stream:
                            while remaining:
                                chunk = process.stdout.read(min(remaining, 1024 * 1024))
                                if not chunk:
                                    raise ValueError(f"Truncated source file blob: {name}")
                                stream.write(chunk)
                                remaining -= len(chunk)
                        path.chmod(0o755 if mode == "100755" else 0o644)
                    if process.stdout.read(1) != b"\n":
                        raise ValueError(f"Malformed source blob framing: {name}")
                process.stdin.close()
                if process.wait():
                    errors.seek(0)
                    raise ValueError("Cannot materialize source: " + errors.read().decode(errors="replace"))
            finally:
                if not process.stdin.closed:
                    process.stdin.close()
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
        files = tracked_source_inventory(checkout, revision, tree)
        require_new(output)
        publish_new_directory(checkout, output)
    return {"source": {"repository": repository, "revision": revision, "tree": tree, "clean": True},
            "source_tree_sha256": source_inventory_sha256(files),
            "path": str(output), "tracked_files": len(files)}


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

    index = tracked_git_entries(root, "ls-files", "--stage", "-z")
    if index != tracked_git_entries(root, "ls-tree", "-r", "-z", "--full-tree", revision):
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
    if output.resolve().is_relative_to(source):
        raise ValueError("SDK output must be outside the source checkout")
    revision = git(source, "rev-parse", "HEAD")
    tree = git(source, "rev-parse", "HEAD^{tree}")
    # Git status normalizes text and can invoke external clean filters. Require
    # exact committed bytes instead, then separately reject untracked inputs.
    source_files = tracked_source_inventory(source, revision, tree)
    if git_bytes(source, "ls-files", "--others", "--exclude-standard", "-z"):
        raise ValueError("Source repository must have no untracked inputs")
    repository = git(source, "remote", "get-url", "origin")
    if not repository:
        raise ValueError("Source repository needs an origin identity")
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
        # No filters, line-ending conversion, worktree files, ignored object
        # caches or local hardlinks survive into the actual build checkout.
        materialize_source(source, checkout, revision)
        for name in ("o", ".cosmocc", "cosmocc"):
            require_new(checkout / name)
        if tracked_source_inventory(checkout, revision, tree) != source_files:
            raise ValueError("Materialized source differs from the selected committed bytes")
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
        publish_new_directory(staged, output)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--sdk", type=Path, required=True)
    materialize = commands.add_parser("materialize", help="Create an isolated exact-blob source checkout")
    materialize.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    materialize.add_argument("--output", type=Path, required=True)
    materialize.add_argument("--revision", help="Full commit ID; defaults to the source repository's HEAD")
    build = commands.add_parser("build")
    build.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--bootstrap-sdk", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            record = verify_runtime_sdk(args.sdk)
        elif args.command == "materialize":
            record = materialize_source(args.source, args.output, args.revision)
        else:
            record = build_runtime_sdk(args.source, args.bootstrap_sdk, args.output, args.jobs)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"source runtime: {error}\n")
    print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
