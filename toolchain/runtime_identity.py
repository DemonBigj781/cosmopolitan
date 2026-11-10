"""Content identity for complete source-built SDKs (not a signed attestation)."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

MANIFEST = "SOURCE_RUNTIME.json"
SOURCE_INPUTS = "SOURCE_INPUTS.json"
ARCHITECTURES = ["x86_64", "aarch64"]
BINUTILS_TOOLS = ("addr2line", "ar", "as", "c++filt", "nm", "objcopy", "objdump", "readelf", "strip")
BINUTILS_ALIASES = {f"bin/{arch}-unknown-cosmo-{tool}": f"{arch}-linux-cosmo-{tool}"
                    for arch in ARCHITECTURES for tool in BINUTILS_TOOLS}
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
        for tool in BINUTILS_TOOLS:
            names += [f"bin/{arch}-linux-cosmo-{tool}", f"bin/{arch}-unknown-cosmo-{tool}"]
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
    for name, target in BINUTILS_ALIASES.items():
        path = Path(root) / name
        if not path.is_symlink() or os.readlink(path) != target:
            raise ValueError(f"Source runtime requires architecture-specific binutils alias: {name}")
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


def require_source_path(name):
    """Keep tracked source names portable and unambiguous in content records."""
    if (not isinstance(name, str) or not name or name.startswith("/") or
            any(c in name for c in ("\\", "\n", "\r", "\x00")) or
            any(part in ("", ".", "..", ".git") for part in name.split("/"))):
        raise ValueError(f"Unsafe tracked source path: {name!r}")


def source_inventory_sha256(files):
    """Hash the entire tracked path/mode/type/content inventory, not a Git ID."""
    return hashlib.sha256(canonical(files)).hexdigest()


def verify_source_inputs(root, record):
    """Verify portable source evidence without requiring the source checkout.

    Older SDKs with no source inventory remain readable. Exporters can require
    the new fields explicitly; incomplete or orphaned evidence is never legacy.
    These records establish local content consistency, not signed provenance.
    """
    root = Path(root)
    fields = ("source_tree_sha256", "source_inventory_record", "source_patches")
    present = [key in record for key in fields]
    path = root / SOURCE_INPUTS
    if not any(present):
        if path.exists() or path.is_symlink():
            raise ValueError("Unbound source input inventory")
        return None
    if not all(present):
        raise ValueError("Incomplete source input inventory evidence")
    reference = record["source_inventory_record"]
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"} or
            reference.get("path") != SOURCE_INPUTS or
            not isinstance(reference.get("sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])):
        raise ValueError("Invalid source input inventory reference")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Source input inventory must be a regular SOURCE_INPUTS.json")
    if digest_file(path) != reference["sha256"]:
        raise ValueError("Source input inventory record digest mismatch")
    inputs = read_json(path)
    if (not isinstance(inputs, dict) or
            set(inputs) != {"schema_version", "kind", "source", "files"} or
            type(inputs.get("schema_version")) is not int or inputs["schema_version"] != 1 or
            inputs.get("kind") != "cosmopolitan-tracked-source-inputs" or
            inputs.get("source") != record["source"]):
        raise ValueError("Source input inventory identity mismatch")
    files = inputs["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("Missing complete tracked source inventory")
    for name, entry in files.items():
        require_source_path(name)
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid tracked source entry: {name}")
        if (entry.get("mode"), entry.get("type")) not in (
                ("100644", "file"), ("100755", "file"), ("120000", "symlink")):
            raise ValueError(f"Unsupported tracked source type or mode: {name}")
        expected = {"mode", "type", "size", "sha256"}
        if entry["type"] == "symlink":
            expected.add("target")
        if (set(entry) != expected or type(entry.get("size")) is not int or entry["size"] < 0 or
                not isinstance(entry.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError(f"Invalid tracked source fingerprint: {name}")
        if entry["type"] == "symlink":
            target = entry["target"]
            if not isinstance(target, str) or not target or "\x00" in target:
                raise ValueError(f"Invalid tracked source symlink: {name}")
            target_bytes = os.fsencode(target)
            if (len(target_bytes) != entry["size"] or
                    hashlib.sha256(target_bytes).hexdigest() != entry["sha256"]):
                raise ValueError(f"Tracked source symlink fingerprint mismatch: {name}")
        parts = name.split("/")
        if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
            raise ValueError(f"Tracked source paths overlap: {name}")
    if record["source_tree_sha256"] != source_inventory_sha256(files):
        raise ValueError("Complete source inventory digest mismatch")
    # This producer builds the pinned commit directly. It applies no patch
    # series afterwards; downstream adapters have their own patch identities.
    if record["source_patches"] != []:
        raise ValueError("Source runtime requires no patches after the pinned commit")
    for name, digest in record["source_abi_inputs"].items():
        if files.get(name, {}).get("type") != "file" or files[name]["sha256"] != digest:
            raise ValueError(f"Source ABI fingerprint differs from complete inventory: {name}")
    producer = record["producer"]
    recipe = files.get(producer["entrypoint"], {})
    if recipe.get("type") != "file" or recipe.get("sha256") != producer.get("sha256"):
        raise ValueError("Package recipe fingerprint differs from complete source inventory")
    return inputs


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
    verify_source_inputs(root, record)
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
