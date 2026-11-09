"""Package the deterministic source/link kit used by ``toolchain.com self-build``."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import zipfile

from runtime_identity import selected_runtime


PREFIX = "self-build/"
MANIFEST_PATH = "self-build/manifest.json"
SOURCE_NAMES = (
    "ape_toolchain.c",
    "embedded-sdk-driver.c",
    "native_zip.c",
    "machin_build.c",
    "project_manifest.c",
    "rust_build.c",
    "self_build.c",
    "tempdir.c",
)
COMPILED_OBJECTS = (
    "compiler.o",
    "ljson.o",
    "lua_entry.o",
    "tempdir.o",
    "rust_build.o",
    "project_manifest.o",
    "machin_build.o",
    "native_zip.o",
    "embedded-sdk-driver.o",
    "self_build.o",
    "ape_toolchain.o",
)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_info(name, mode):
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def run_lua_deps(repo, make, mode):
    result = subprocess.run(
        [make, "-s", "-j1", "-f", "Makefile", "-f", "toolchain/lua-link.mk",
         "toolchain-lua-link", f"MODE={mode}"],
        cwd=repo, check=True, text=True, stdout=subprocess.PIPE,
    )
    return result.stdout.split()


def source_dependencies(repo, cosmocc_root, source, arch="x86_64", machin=False):
    if arch not in ("x86_64", "aarch64"):
        raise ValueError(f"Unsupported self-build architecture: {arch}")
    compiler = cosmocc_root / f"bin/{arch}-linux-cosmo-gcc"
    mode = "aarch64" if arch == "aarch64" else ""
    command = [
        "sh", str(compiler), "-M", "-D_COSMO_SOURCE", f'-DMODE="{mode}"',
        "-nostdinc", "-iquote", str(repo), "-isystem", str(repo / "libc/isystem"),
        "-include", "libc/integral/normalize.inc", "-std=gnu23",
    ]
    if machin:
        command.extend(("-std=c11", "-pthread", "-Dmain=machin_entry"))
    command.append(str(source))
    output = subprocess.run(
        command, cwd=repo, check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.replace("\\\n", " ")
    dependencies = []
    for value in shlex.split(output.split(":", 1)[1]):
        path = Path(value)
        if not path.is_absolute():
            path = repo / path
        path = path.resolve()
        if path.is_file() and path.is_relative_to(repo):
            dependencies.append(path)
    return dependencies


def normalize_output_path(path, mode):
    value = path.replace("//", "/")
    prefix = "o/aarch64/" if mode == "aarch64" else "o/"
    if not value.startswith(prefix):
        raise ValueError(f"Unexpected {mode or 'x86_64'} dependency: {path}")
    return value[len(prefix):]


def machin_mode(machin_source=None, machin_object=None, machin_arm_object=None):
    if machin_source is not None and (machin_object is not None or machin_arm_object is not None):
        raise ValueError("Machin source and prebuilt objects are mutually exclusive")
    if (machin_object is None) != (machin_arm_object is None):
        raise ValueError("Prebuilt Machin requires both --machin-object and --machin-arm-object")
    if machin_source is not None:
        return "source"
    return "prebuilt" if machin_object is not None else "absent"


def collect_entries(repo, cosmocc_root, make, *, machin_source=None,
                    machin_object=None, machin_arm_object=None):
    machin_kind = machin_mode(machin_source, machin_object, machin_arm_object)
    entries = {}

    def add_path(name, path, mode=None):
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        file_mode = mode if mode is not None else (0o755 if os.access(path, os.X_OK) else 0o644)
        if name in entries:
            if entries[name] != (path, file_mode):
                raise ValueError(f"Conflicting self-build archive path: {name}")
            return
        entries[name] = (path, file_mode)

    sources = [repo / "toolchain" / name for name in SOURCE_NAMES]
    if machin_source is not None:
        machin_source = machin_source.resolve()
        # The native builder compiles this fixed path. Require an explicit
        # opt-in, never infer source mode from an old build's compiler.c.
        expected_source = repo / "o/toolchain/compiler.c"
        if machin_source != expected_source or not machin_source.is_relative_to(repo):
            raise ValueError(f"--machin-source must be the generated {expected_source}")
        if not machin_source.is_file():
            raise FileNotFoundError(machin_source)
        sources.append(machin_source)
    source_files = set(sources)
    for source in sources:
        for arch in ("x86_64", "aarch64"):
            source_files.update(source_dependencies(
                repo, cosmocc_root, source, arch=arch, machin=source == machin_source,
            ))
    for path in sorted(source_files):
        add_path(PREFIX + "source/" + path.relative_to(repo).as_posix(), path)

    for name in ("tlscc", "fixupobj"):
        add_path(PREFIX + "tools/" + name, repo / "build/bootstrap" / name, 0o755)

    object_paths = {
        "x86_64": {
            "lua_entry.o": repo / "o/toolchain/lua_entry.o",
            "ljson.o": repo / "o/tool/net/ljson.o",
        },
        "aarch64": {
            "lua_entry.o": repo / "o/toolchain/.aarch64/lua_entry.o",
            "ljson.o": repo / "o/tool/net/.aarch64/ljson.o",
        },
    }
    if machin_kind == "prebuilt":
        object_paths["x86_64"]["compiler.o"] = machin_object
        object_paths["aarch64"]["compiler.o"] = machin_arm_object
    for arch, objects in object_paths.items():
        for name, path in objects.items():
            add_path(PREFIX + f"objects/{arch}/{name}", path)

    dependency_paths = {}
    for arch, mode in (("x86_64", ""), ("aarch64", "aarch64")):
        dependencies = run_lua_deps(repo, make, mode)
        dependency_paths[arch] = []
        for path_string in dependencies:
            relative = normalize_output_path(path_string, mode)
            archive_name = PREFIX + f"deps/{arch}/{relative}"
            add_path(archive_name, repo / path_string)
            dependency_paths[arch].append(archive_name[len(PREFIX):])

    linker_inputs = {
        "x86_64": (
            ("o//ape/ape-no-modify-self.o", "ape/ape-no-modify-self.o"),
            ("o//ape/ape.lds", "ape/ape.lds"),
            ("o//libc/crt/crt.o", "libc/crt/crt.o"),
        ),
        "aarch64": (
            ("o/aarch64/ape/aarch64.lds", "ape/aarch64.lds"),
            ("o/aarch64/libc/crt/crt.o", "libc/crt/crt.o"),
        ),
    }
    for arch, inputs in linker_inputs.items():
        for source, relative in inputs:
            add_path(PREFIX + f"deps/{arch}/{relative}", repo / source)

    common = [
        "-static", "-nostdlib", "-znorelro", "--gc-sections", "-z", "noexecstack",
        "--build-id=none", "--no-dynamic-linker", "-zmax-page-size=0x4000",
    ]
    responses = {}
    for arch in ("x86_64", "aarch64"):
        args = list(common)
        if arch == "x86_64":
            args.extend(("-zcommon-page-size=0x1000", "deps/x86_64/ape/ape-no-modify-self.o",
                         "-T", "deps/x86_64/ape/ape.lds", "deps/x86_64/libc/crt/crt.o"))
        else:
            args.extend(("-zcommon-page-size=0x4000", "-znorelro", "-T",
                         "deps/aarch64/ape/aarch64.lds", "deps/aarch64/libc/crt/crt.o"))
        for name in COMPILED_OBJECTS:
            if name == "compiler.o" and machin_kind == "absent":
                continue
            if name in object_paths[arch]:
                args.append(f"objects/{arch}/{name}")
            else:
                args.append(f"work/{arch}/{name}")
        args.extend(reversed(dependency_paths[arch]))
        responses[PREFIX + f"link/{arch}.rsp"] = ("\n".join(args) + "\n").encode()
    return entries, responses


def verify_kit(path, manifest, manifest_data):
    """Check the bytes actually archived, not a second view of build inputs."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        expected_names = {entry["path"] for entry in manifest["entries"]}
        expected_names.add(MANIFEST_PATH)
        if len(names) != len(set(names)) or set(names) != expected_names:
            raise ValueError("Self-build kit contains duplicate, missing, or unexpected paths")
        if archive.read(MANIFEST_PATH) != manifest_data:
            raise ValueError("Self-build kit manifest changed during packaging")
        for entry in manifest["entries"]:
            info = archive.getinfo(entry["path"])
            digest = hashlib.sha256()
            size = 0
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
            if (digest.hexdigest() != entry["sha256"] or size != entry["size"] or
                    info.file_size != entry["size"] or info.create_system != 3 or
                    info.external_attr >> 16 != (stat.S_IFREG | entry["mode"])):
                raise ValueError(f"Self-build kit inventory mismatch: {entry['path']}")


def write_kit(repo, cosmocc_root, output, make, *, machin_source=None,
              machin_object=None, machin_arm_object=None):
    repo = repo.resolve()
    machin_kind = machin_mode(machin_source, machin_object, machin_arm_object)
    runtime_root = cosmocc_root
    runtime = selected_runtime(runtime_root)
    cosmocc_root = cosmocc_root.resolve()
    entries, generated = collect_entries(
        repo, cosmocc_root, make, machin_source=machin_source,
        machin_object=machin_object, machin_arm_object=machin_arm_object,
    )
    inventory = []
    tree = hashlib.sha256()
    for name in sorted(entries):
        path, mode = entries[name]
        digest = sha256_file(path)
        size = path.stat().st_size
        inventory.append({"path": name, "sha256": digest, "size": size, "mode": mode})
        tree.update(f"{digest} {size} {mode:o} {name}\n".encode())
    for name in sorted(generated):
        data = generated[name]
        digest = sha256_bytes(data)
        inventory.append({"path": name, "sha256": digest, "size": len(data), "mode": 0o644})
        tree.update(f"{digest} {len(data)} 644 {name}\n".encode())
    manifest = {
        "schema_version": 1,
        "target": "cosmopolitan-fat-ape-x86_64-aarch64",
        "machin_mode": machin_kind,
        "runtime": runtime,
        "kit_tree_sha256": tree.hexdigest(),
        "entry_count": len(inventory),
        "entries": inventory,
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                allowZip64=False, strict_timestamps=False,
            ) as archive:
                for name in sorted(entries):
                    path, file_mode = entries[name]
                    archive.writestr(archive_info(name, file_mode), path.read_bytes())
                for name in sorted(generated):
                    archive.writestr(archive_info(name, 0o644), generated[name])
                archive.writestr(archive_info(MANIFEST_PATH, 0o644), manifest_data)
        verify_kit(temporary, manifest, manifest_data)
        if selected_runtime(runtime_root) != runtime:
            raise ValueError("Self-build runtime identity changed during packaging")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"self-build kit: {output} files: {len(inventory) + 1} bytes: {output.stat().st_size}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--cosmocc-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--make", default="make")
    parser.add_argument("--machin-source", type=Path,
                        help="Recompile this repository's generated o/toolchain/compiler.c")
    parser.add_argument("--machin-object", type=Path,
                        help="Preserve an external prebuilt x86-64 Machin object")
    parser.add_argument("--machin-arm-object", type=Path,
                        help="Preserve its external prebuilt AArch64 Machin object")
    args = parser.parse_args()
    try:
        machin_mode(args.machin_source, args.machin_object, args.machin_arm_object)
    except ValueError as error:
        parser.error(str(error))
    write_kit(args.repo, args.cosmocc_root, args.output, args.make,
              machin_source=args.machin_source, machin_object=args.machin_object,
              machin_arm_object=args.machin_arm_object)


if __name__ == "__main__":
    main()
