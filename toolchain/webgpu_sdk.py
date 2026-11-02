#!/usr/bin/env python3
"""Stage a relocatable x86-64 WebGPU/Mesa/LLVM SDK for the manifest toolchain."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ValueError(f"missing WebGPU SDK input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def archive_destination(path: Path) -> Path:
    parts = path.parts
    if "llvm-cosmo" in parts:
        return Path("lib/llvm") / path.name
    if "lavapipe" in parts:
        return Path("lib/mesa") / path.name
    raise ValueError(f"unrecognized WebGPU link archive: {path}")


def relocated_build_path(path: Path, repository_root: Path = ROOT) -> Path:
    if path.exists():
        return path
    marker = f"{Path('/').anchor}o{Path('/').anchor}"
    text = str(path)
    if marker in text:
        candidate = repository_root / "o" / text.split(marker, 1)[1]
        if candidate.exists():
            return candidate
    return path


def copy_tree(source: Path, destination: Path, hashes: dict[str, str],
              stage: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"missing WebGPU SDK input directory: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"WebGPU SDK license tree contains a symlink: {path}")
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        copy_file(path, target)
        hashes[target.relative_to(stage).as_posix()] = sha256(path)


def stage_webgpu_sdk(link_manifest: Path, wgpu_archive: Path,
                     rust_sdk: Path, output: Path,
                     repository_root: Path = ROOT) -> dict:
    repository_root = repository_root.resolve(strict=True)
    link_manifest = link_manifest.resolve(strict=True)
    wgpu_archive = wgpu_archive.resolve(strict=True)
    rust_sdk = rust_sdk.resolve(strict=True)
    output = output.resolve()
    link = json.loads(link_manifest.read_text())
    if link.get("static") is not True or link.get("external_vulkan_loader") is not False:
        raise ValueError("WebGPU SDK requires the static embedded lavapipe manifest")
    arguments = link.get("link_args")
    if not isinstance(arguments, list) or not arguments:
        raise ValueError("lavapipe LINK.json has no link arguments")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="webgpu-sdk-", dir=output.parent) as temporary:
        stage = Path(temporary) / "webgpu-sdk"
        staged_arguments = ["lib/wgpu/libwgpu_native.a"]
        hashes: dict[str, str] = {}
        copied: dict[Path, Path] = {}

        wgpu_destination = Path("lib/wgpu/libwgpu_native.a")
        copy_file(wgpu_archive, stage / wgpu_destination)
        hashes[wgpu_destination.as_posix()] = sha256(wgpu_archive)

        for argument in arguments:
            if not isinstance(argument, str) or not argument or "\0" in argument or "\n" in argument:
                raise ValueError("invalid lavapipe link argument")
            if argument.startswith("-"):
                if any(token in argument for token in (".so", ".dll", ".dylib")):
                    raise ValueError(f"shared WebGPU link input is not portable: {argument}")
                staged_arguments.append(argument)
                continue
            source = relocated_build_path(Path(argument), repository_root)
            if not source.is_absolute() or source.suffix != ".a":
                raise ValueError(f"expected an absolute static archive: {argument}")
            destination = copied.get(source)
            if destination is None:
                destination = archive_destination(source)
                if (stage / destination).exists():
                    raise ValueError(f"duplicate WebGPU archive name: {destination}")
                copy_file(source, stage / destination)
                copied[source] = destination
                hashes[destination.as_posix()] = sha256(source)
            staged_arguments.append(destination.as_posix())

        shim_directory = rust_sdk / "generated"
        shims = sorted(shim_directory.glob("shim-*-x86_64.o"))
        if not shims:
            raise ValueError("Rust-APE x86-64 shim objects are missing")
        for source in shims:
            destination = Path("lib/rust-ape") / source.name
            copy_file(source, stage / destination)
            hashes[destination.as_posix()] = sha256(source)
            staged_arguments.append(destination.as_posix())
        staged_arguments.extend((
            "-Wl,--defsym,__isoc23_strtol=strtol",
            "-Wl,--defsym,__isoc23_sscanf=sscanf",
        ))

        source_files = {
            repository_root / "third_party/wgpu_native/upstream/ffi/wgpu.h": Path("include/wgpu.h"),
            repository_root / "third_party/wgpu_native/upstream/ffi/webgpu-headers/webgpu.h": Path("include/webgpu.h"),
            repository_root / "third_party/wgpu_native/runtime/vulkan_loader.c": Path("runtime/vulkan_loader.c"),
            repository_root / "third_party/wgpu_native/runtime/vulkan_loader.h": Path("runtime/vulkan_loader.h"),
            repository_root / "third_party/wgpu_native/runtime/win64_bridge.c": Path("runtime/win64_bridge.c"),
            repository_root / "third_party/wgpu_native/runtime/win64_bridge.h": Path("runtime/win64_bridge.h"),
            repository_root / "third_party/lavapipe/register.c": Path("runtime/lavapipe_register.c"),
            repository_root / "third_party/lavapipe/register.h": Path("include/cosmo_lavapipe.h"),
            repository_root / "o/webgpu/generated/vulkan_signatures.inc": Path("runtime/vulkan_signatures.inc"),
        }
        for source, destination in source_files.items():
            copy_file(source, stage / destination)
            hashes[destination.as_posix()] = sha256(source)

        licenses = {
            repository_root / "third_party/wgpu_native/upstream/LICENSE.APACHE": Path("licenses/wgpu-native/LICENSE.APACHE"),
            repository_root / "third_party/wgpu_native/upstream/LICENSE.MIT": Path("licenses/wgpu-native/LICENSE.MIT"),
            repository_root / "third_party/wgpu_native/upstream/ffi/webgpu-headers/LICENSE": Path("licenses/webgpu-headers/LICENSE"),
            repository_root / "o/webgpu/NOTICE.txt": Path("licenses/wgpu-native/NOTICE.txt"),
        }
        for source, destination in licenses.items():
            copy_file(source, stage / destination)
            hashes[destination.as_posix()] = sha256(source)
        copy_tree(repository_root / "o/webgpu/licenses",
                  stage / "licenses/wgpu-dependencies", hashes, stage)
        lavapipe_licenses = relocated_build_path(
            Path(link.get("licenses", "")), repository_root)
        copy_tree(lavapipe_licenses, stage / "licenses/software-vulkan",
                  hashes, stage)

        (stage / "link.args").write_text("".join(argument + "\n" for argument in staged_arguments))
        metadata = {
            "format": 1,
            "target": "x86_64 Cosmopolitan APE",
            "wgpu_native": "v29.0.1.1",
            "mesa": link.get("mesa_version"),
            "llvm": link.get("llvm_version"),
            "provider": link.get("provider"),
            "capabilities": ["compute", "offscreen-rgba8-raster", "texture-readback"],
            "link_arguments": len(staged_arguments),
            "files": dict(sorted(hashes.items())),
        }
        (stage / "manifest.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        if output.exists():
            if output.is_symlink() or not output.is_dir():
                raise ValueError(f"refusing to replace WebGPU SDK path: {output}")
            shutil.rmtree(output)
        stage.replace(output)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link-manifest", type=Path, required=True)
    parser.add_argument("--wgpu-archive", type=Path, required=True)
    parser.add_argument("--rust-sdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = stage_webgpu_sdk(args.link_manifest, args.wgpu_archive,
                                args.rust_sdk, args.output)
    print(f"WebGPU SDK staged: {args.output} ({len(metadata['files'])} hashed files)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"WebGPU SDK staging failed: {error}") from error
