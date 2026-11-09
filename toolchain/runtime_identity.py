"""Content identity for complete source-built SDKs (not a signed attestation)."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

MANIFEST = "SOURCE_RUNTIME.json"
ARCHITECTURES = ["x86_64", "aarch64"]
VARIANTS = ("", "dbg", "tiny", "sysv", "optlinux", "tinylinux")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inventory(root, exclude=()):
    """Inventory without following directory links; reject links escaping the SDK."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Expected real SDK directory: {root}")
    root = root.resolve()
    result = {}

    def visit(directory):
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            if name in exclude:
                continue
            if any(c in name for c in ("\\", "\n", "\r", "\x00")):
                raise ValueError(f"Unsafe SDK path: {name!r}")
            mode = path.lstat().st_mode
            entry = {"mode": stat.S_IMODE(mode)}
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if not target or os.path.isabs(target) or "\\" in target:
                    raise ValueError(f"Unsafe SDK symlink: {name}")
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                except (ValueError, OSError, RuntimeError) as error:
                    raise ValueError(f"Escaping, cyclic or dangling SDK symlink: {name}") from error
                entry.update(type="symlink", target=target,
                             sha256=hashlib.sha256(target.encode()).hexdigest())
            elif stat.S_ISDIR(mode):
                entry.update(type="directory")
                visit(path)
            elif stat.S_ISREG(mode):
                entry.update(type="file", size=path.stat().st_size, sha256=digest_file(path))
            else:
                raise ValueError(f"Unsupported SDK file: {name}")
            result[name] = entry
    visit(root)
    return dict(sorted(result.items()))


def required_files():
    names = ["include/stdio.h", "include/pthread.h", "bin/cosmocc", "bin/cosmoc++",
             "bin/apelink", "bin/ape-x86_64.elf", "bin/ape-x86_64.macho",
             "bin/ape-aarch64.elf", "bin/ape-m1.c"]
    for arch in ARCHITECTURES:
        names += [f"bin/{arch}-linux-cosmo-gcc", f"bin/{arch}-linux-cosmo-g++"]
        for variant in VARIANTS:
            prefix = f"{arch}-linux-cosmo/lib/" + (variant + "/" if variant else "")
            leaves = ["crt.o", "crtfastmath.o", "libcosmo.a", "libcxx.a"]
            leaves += ["ape.o", "ape-no-modify-self.o", "ape.lds"] if arch == "x86_64" else ["aarch64.lds"]
            names += [prefix + leaf for leaf in leaves]
    return names


def require_layout(root):
    for name in required_files():
        if not (Path(root) / name).is_file():
            raise ValueError(f"Incomplete source runtime SDK: {name}")
    for path in Path(root).glob("*-linux-cosmo"):
        if path.name not in {a + "-linux-cosmo" for a in ARCHITECTURES}:
            raise ValueError(f"Unsupported runtime architecture: {path.name}")


def runtime_identity(record):
    return "sha256:" + hashlib.sha256(canonical({k: v for k, v in record.items()
                                                 if k != "runtime_identity"})).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate manifest key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)


def verify_runtime_sdk(sdk_root):
    root = Path(sdk_root)
    path = root / MANIFEST
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("Source runtime SDK requires a regular SOURCE_RUNTIME.json")
    record = read_json(path)
    if (not isinstance(record, dict) or record.get("schema_version") != 1 or
            record.get("kind") != "source-built-cosmopolitan-runtime" or
            record.get("architectures") != ARCHITECTURES or
            record.get("runtime_abi") != "cosmopolitan-source-runtime-v1"):

        raise ValueError("Unsupported source runtime manifest or architectures")
    source = record.get("source", {})
    if not isinstance(source, dict):
        raise ValueError("Invalid source identity")
    for key in ("revision", "tree"):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(source.get(key, ""))):
            raise ValueError(f"Invalid source {key}")
    if not source.get("repository") or source.get("clean") is not True:
        raise ValueError("Source runtime requires clean committed source")
    bootstrap = record.get("bootstrap", {})
    if not isinstance(bootstrap, dict):
        raise ValueError("Invalid bootstrap identity")
    if (bootstrap.get("role") != "stock-compiler-input-only" or
            not re.fullmatch(r"[0-9a-f]{64}", str(bootstrap.get("tree_sha256", ""))) or
            not re.fullmatch(r"[0-9a-f]{64}", str(bootstrap.get("archive_sha256", "")))):
        raise ValueError("Invalid bootstrap identity")
    abi = record.get("source_abi_inputs", {})
    expected_abi = {"libc/nt/struct/sid.h", "libc/dlopen/dladdr.c",
                    "libc/intrin/fds.h", "libc/proc/posix_spawn.c"}
    if not isinstance(abi, dict) or set(abi) != expected_abi or any(
            not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in abi.values()):
        raise ValueError("Missing source ABI fingerprints")
    producer = record.get("producer", {})
    if not isinstance(producer, dict):
        raise ValueError("Invalid producer identity")
    if (producer.get("entrypoint") != "tool/cosmocc/package.sh" or
            producer.get("fresh_outputs") is not True):
        raise ValueError("SDK did not record a fresh complete package build")
    if record.get("runtime_identity") != runtime_identity(record):
        raise ValueError("Source runtime manifest identity mismatch")
    if record.get("files") != inventory(root, exclude=(MANIFEST,)):
        raise ValueError("Source runtime files changed, missing, or mixed with other inputs")
    require_layout(root)
    return record


def selected_runtime(sdk, profile=None):
    """Select an explicit whole-runtime profile without silently mixing SDKs.

    Legacy stock callers keep a minimal deterministic record: absence of a
    source manifest is not evidence of a particular stock SDK version. Source
    callers receive the verified identity of the entire tree, not just libc.a.
    """
    profile = profile if profile is not None else os.environ.get("COSMO_RUNTIME_PROFILE", "stock-sdk")
    if profile not in ("stock-sdk", "source-built"):
        raise ValueError("COSMO_RUNTIME_PROFILE must be stock-sdk or source-built")
    root = Path(sdk)
    if profile == "source-built":
        record = verify_runtime_sdk(root)
        return {key: record[key] for key in ("kind", "source", "runtime_identity", "runtime_abi")}
    manifest = root / MANIFEST
    # lexists semantics also reject dangling links and other invalid manifest
    # entries rather than allowing them to turn a source SDK into stock.
    if manifest.exists() or manifest.is_symlink():
        raise ValueError("Source runtime SDK requires explicit source-built profile")
    return {"kind": "stock-sdk"}
