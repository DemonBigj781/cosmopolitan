"""Package the deterministic source/link kit used by ``toolchain.com self-build``."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import zipfile


PREFIX = "self-build/"
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


def source_dependencies(repo, cosmocc_root, source):
    compiler = cosmocc_root / "bin/x86_64-linux-cosmo-gcc"
    command = [
        "sh", str(compiler), "-M", "-D_COSMO_SOURCE", '-DMODE=""',
        "-nostdinc", "-iquote", str(repo), "-isystem", str(repo / "libc/isystem"),
        "-include", "libc/integral/normalize.inc", "-std=gnu23",
    ]
    if source.name == "compiler.c":
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


def collect_entries(repo, cosmocc_root, make):
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
    sources.append(repo / "o/toolchain/compiler.c")
    source_files = set(sources)
    for source in sources:
        source_files.update(source_dependencies(repo, cosmocc_root, source))
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
            if name in ("ljson.o", "lua_entry.o"):
                args.append(f"objects/{arch}/{name}")
            else:
                args.append(f"work/{arch}/{name}")
        args.extend(reversed(dependency_paths[arch]))
        responses[PREFIX + f"link/{arch}.rsp"] = ("\n".join(args) + "\n").encode()
    return entries, responses


def write_kit(repo, cosmocc_root, output, make):
    repo = repo.resolve()
    cosmocc_root = cosmocc_root.resolve()
    entries, generated = collect_entries(repo, cosmocc_root, make)
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
        "kit_tree_sha256": tree.hexdigest(),
        "entry_count": len(inventory),
        "entries": inventory,
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
        allowZip64=False, strict_timestamps=False,
    ) as archive:
        for name in sorted(entries):
            path, mode = entries[name]
            archive.writestr(archive_info(name, mode), path.read_bytes())
        for name in sorted(generated):
            archive.writestr(archive_info(name, 0o644), generated[name])
        archive.writestr(archive_info("self-build/manifest.json", 0o644), manifest_data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Self-build kit failed its CRC check")
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Self-build kit contains duplicate paths")
    print(f"self-build kit: {output} files: {len(inventory) + 1} bytes: {output.stat().st_size}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--cosmocc-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--make", default="make")
    args = parser.parse_args()
    write_kit(args.repo, args.cosmocc_root, args.output, args.make)


if __name__ == "__main__":
    main()
