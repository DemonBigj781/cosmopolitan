#!/usr/bin/env python3
"""Stage a relocatable x86-64 WebGPU/Mesa/LLVM SDK for the manifest toolchain."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

from runtime_identity import selected_runtime
from static_archives import archive_sha256, verify_archives


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/wgpu_native"))
from source_receipt import file_sha256, tree_receipt, verify_prepared


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(source: Path, destination: Path, expected: str | None = None) -> str:
    if not source.is_file():
        raise ValueError(f"missing WebGPU SDK input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    actual = sha256(destination)
    if expected is not None and actual != expected:
        raise ValueError(f"WebGPU SDK input changed while copying: {source}")
    return actual


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
        hashes[target.relative_to(stage).as_posix()] = copy_file(path, target)


def dependency_hashes(arguments: list[str], manifest: dict,
                      repository_root: Path, required: bool) -> dict[str, str]:
    """Check producer hashes before relocating paths; never relabel new bytes."""
    archives = [argument for argument in arguments if not argument.startswith("-")]
    if not archives or any(not Path(arg).is_absolute() or Path(arg).suffix != ".a"
                           for arg in archives):
        raise ValueError("expected absolute static lavapipe archives")
    expected = manifest.get("link_archive_sha256")
    if expected is None and not required:
        # Legacy stock fixtures predate archive inventories. They retain their
        # old acceptance rules but still hash the bytes actually staged below.
        return {}
    if not isinstance(expected, dict) or set(expected) != set(archives):
        raise ValueError("lavapipe link archive hash inventory must cover every archive")
    relocated = {}
    for argument in archives:
        path = str(relocated_build_path(Path(argument), repository_root))
        if path in relocated and relocated[path] != expected[argument]:
            raise ValueError(f"conflicting relocated archive hashes: {path}")
        relocated[path] = expected[argument]
    return verify_archives(list(relocated), relocated, required=True)


def stage_webgpu_sdk(link_manifest: Path, wgpu_archive: Path,
                     rust_sdk: Path, output: Path,
                     repository_root: Path = ROOT,
                     build_manifest: Path | None = None) -> dict:
    repository_root = repository_root.resolve(strict=True)
    link_manifest = link_manifest.resolve(strict=True)
    wgpu_archive = wgpu_archive.resolve(strict=True)
    rust_sdk = rust_sdk.resolve(strict=True)
    output = output.resolve()
    link = json.loads(link_manifest.read_text())
    if not isinstance(link, dict):
        raise ValueError("lavapipe LINK.json must be an object")
    runtime = selected_runtime(rust_sdk / "vendor/cosmocc")
    source_runtime = runtime["kind"] != "stock-sdk"
    wgpu_hash = archive_sha256(wgpu_archive) if source_runtime else sha256(wgpu_archive)
    if link.get("runtime", {"kind": "stock-sdk"}) != runtime:
        raise ValueError("lavapipe LINK.json runtime does not match the Rust SDK")
    if build_manifest is None:
        build_manifest = repository_root / "o/webgpu/BUILD.json"
    build = {}
    if build_manifest.exists():
        build = json.loads(build_manifest.read_text())
        if not isinstance(build, dict):
            raise ValueError("WebGPU BUILD.json must be an object")
        if build.get("runtime", {"kind": "stock-sdk"}) != runtime:
            raise ValueError("WebGPU BUILD.json runtime does not match the Rust SDK")
        if "wgpu_archive_sha256" in build and build["wgpu_archive_sha256"] != wgpu_hash:
            raise ValueError("WebGPU archive does not match BUILD.json")
    elif source_runtime:
        raise ValueError("source-built WebGPU SDK requires BUILD.json")
    if source_runtime:
        # Verify the staged profile and regenerated ABI tables, not just the C
        # SDK copied into vendor/. This performs no builds or target execution.
        sys.path.insert(0, str(ROOT / "third_party/rust_ape"))
        from runtime_profile import expected_shim_objects, verify_binding
        binding = verify_binding(rust_sdk)
        shims = expected_shim_objects(rust_sdk)
        if (binding.get("runtime_identity") != runtime["runtime_identity"] or
                binding.get("runtime_abi") != runtime["runtime_abi"]):
            raise ValueError("Rust shim binding does not match the selected runtime")
        if build.get("wgpu_archive_sha256") != wgpu_hash:
            raise ValueError("source-built WebGPU archive requires its BUILD.json hash")
        if build.get("rust_shim_sha256") != {path.name: sha256(path) for path in shims}:
            raise ValueError("Rust shim objects do not match WebGPU BUILD.json")
        software = build.get("software_vulkan")
        if (build.get("embedded_software_vulkan") is not True or
                not isinstance(software, dict) or software.get("runtime") != runtime):
            raise ValueError("WebGPU BUILD.json requires matching embedded Mesa runtime metadata")
        if software != link:
            raise ValueError("lavapipe LINK.json does not match the closure recorded in WebGPU BUILD.json")
    elif (rust_sdk / ".cosmo-runtime-binding.json").exists():
        raise ValueError("source-built Rust binding cannot be packaged as stock-sdk")
    else:
        shims = sorted((rust_sdk / "generated").glob("shim-*-x86_64.o"))
    if link.get("static") is not True or link.get("external_vulkan_loader") is not False:
        raise ValueError("WebGPU SDK requires the static embedded lavapipe manifest")
    arguments = link.get("link_args")
    if not isinstance(arguments, list) or not arguments:
        raise ValueError("lavapipe LINK.json has no link arguments")
    if any(not isinstance(arg, str) or not arg or "\0" in arg or "\n" in arg for arg in arguments):
        raise ValueError("invalid lavapipe link argument")
    dependency_hashes(arguments, link, repository_root, source_runtime)
    expected_archives = link.get("link_archive_sha256")

    # Legacy stock fixtures have no source receipt. Every source-runtime build
    # and every new/explicitly application-patched build must bind prepared
    # headers, rather than silently substituting the pristine public API.
    receipted = source_runtime or any(key in build for key in (
        "prepared_receipt_sha256", "prepared_source_sha256", "application_patches",
        "application_patch_order"))
    prepared = None
    prepared_hash = None
    build_hash = None
    webgpu_output = repository_root / "o/webgpu"
    native_source = repository_root / "third_party/wgpu_native/upstream"
    if receipted:
        prepared_hash = build.get("prepared_receipt_sha256")
        if (not isinstance(prepared_hash, str) or len(prepared_hash) != 64 or
                any(char not in "0123456789abcdef" for char in prepared_hash)):
            raise ValueError("source/application-patched WebGPU SDK requires a prepared receipt hash")
        webgpu_output = build_manifest.parent
        build_hash = file_sha256(build_manifest)
        if json.loads(build_manifest.read_text()) != build:
            raise ValueError("WebGPU BUILD.json changed before SDK staging")
        prepared, _ = verify_prepared(webgpu_output,
                                      repository_root / "third_party/wgpu_native", prepared_hash)
        if (build.get("application_patches") != prepared["application_patches"] or
                build.get("application_patch_order") != prepared["application_patch_order"] or
                build.get("prepared_source_sha256") != prepared["prepared_source_sha256"] or
                build.get("prepared_lock_sha256") != prepared["prepared_lock_sha256"] or
                build.get("foundation_inputs") != prepared["foundation_inputs"]):
            raise ValueError("WebGPU BUILD.json differs from the prepared source receipt")
        if build.get("wgpu_archive_sha256") != wgpu_hash:
            raise ValueError("receipted WebGPU SDK requires its BUILD.json archive hash")
        if build.get("application_patch_licenses_path") != "licenses/application-patches":
            raise ValueError("WebGPU build lacks its application patch notice path")
        if tree_receipt(webgpu_output / "licenses/application-patches") != prepared["application_patch_provenance"]:
            raise ValueError("WebGPU packaged application patch provenance differs from preparation")
        native_source = webgpu_output / "source"

    def prepared_digest(source: Path) -> str | None:
        if prepared is None:
            return None
        name = source.relative_to(native_source).as_posix()
        entry = prepared["source_trees"]["source"]["entries"].get(name)
        if not isinstance(entry, dict) or entry.get("kind") != "file":
            raise ValueError(f"WebGPU input is absent from the prepared source receipt: {name}")
        return entry["sha256"]

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="webgpu-sdk-", dir=output.parent) as temporary:
        stage = Path(temporary) / "webgpu-sdk"
        staged_arguments = ["lib/wgpu/libwgpu_native.a"]
        hashes: dict[str, str] = {}
        copied: dict[Path, Path] = {}

        wgpu_destination = Path("lib/wgpu/libwgpu_native.a")
        hashes[wgpu_destination.as_posix()] = copy_file(
            wgpu_archive, stage / wgpu_destination, build.get("wgpu_archive_sha256"))

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
                expected = expected_archives[argument] if expected_archives is not None else None
                hashes[destination.as_posix()] = copy_file(
                    source, stage / destination, expected)
                copied[source] = destination
            staged_arguments.append(destination.as_posix())

        if not shims:
            raise ValueError("Rust-APE x86-64 shim objects are missing")
        for source in shims:
            destination = Path("lib/rust-ape") / source.name
            expected = build["rust_shim_sha256"][source.name] if source_runtime else None
            hashes[destination.as_posix()] = copy_file(source, stage / destination, expected)
            staged_arguments.append(destination.as_posix())
        staged_arguments.extend((
            "-Wl,--defsym,__isoc23_strtol=strtol",
            "-Wl,--defsym,__isoc23_sscanf=sscanf",
        ))

        source_files = {
            native_source / "ffi/wgpu.h": Path("include/wgpu.h"),
            native_source / "ffi/webgpu-headers/webgpu.h": Path("include/webgpu.h"),
            repository_root / "third_party/wgpu_native/runtime/vulkan_loader.c": Path("runtime/vulkan_loader.c"),
            repository_root / "third_party/wgpu_native/runtime/vulkan_loader.h": Path("runtime/vulkan_loader.h"),
            repository_root / "third_party/wgpu_native/runtime/win64_bridge.c": Path("runtime/win64_bridge.c"),
            repository_root / "third_party/wgpu_native/runtime/win64_bridge.h": Path("runtime/win64_bridge.h"),
            repository_root / "third_party/lavapipe/register.c": Path("runtime/lavapipe_register.c"),
            repository_root / "third_party/lavapipe/register.h": Path("include/cosmo_lavapipe.h"),
            webgpu_output / "generated/vulkan_signatures.inc": Path("runtime/vulkan_signatures.inc"),
        }
        for source, destination in source_files.items():
            expected = None
            if source.is_relative_to(native_source):
                expected = prepared_digest(source)
            elif prepared is not None and source == webgpu_output / "generated/vulkan_signatures.inc":
                expected = prepared["generated_inputs"]["generated/vulkan_signatures.inc"]
            hashes[destination.as_posix()] = copy_file(source, stage / destination, expected)

        licenses = {
            native_source / "LICENSE.APACHE": Path("licenses/wgpu-native/LICENSE.APACHE"),
            native_source / "LICENSE.MIT": Path("licenses/wgpu-native/LICENSE.MIT"),
            native_source / "ffi/webgpu-headers/LICENSE": Path("licenses/webgpu-headers/LICENSE"),
        }
        # Keep any additional application-provided author/license notices in
        # the prepared native/header roots without replacing upstream licensing.
        if prepared is not None:
            for name, entry in prepared["source_trees"]["source"]["entries"].items():
                relative = Path(name)
                if (entry["kind"] == "file" and relative.parent in (Path("."), Path("ffi/webgpu-headers"))
                        and relative.name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "COPYRIGHT"))):
                    directory = "wgpu-native" if relative.parent == Path(".") else "webgpu-headers"
                    licenses[native_source / relative] = Path("licenses") / directory / relative.name
        for source, destination in licenses.items():
            hashes[destination.as_posix()] = copy_file(source, stage / destination, prepared_digest(source))
        # The producer's build notice has a separate name to avoid overwriting
        # an application-provided source NOTICE.txt.
        build_notice = Path("licenses/wgpu-native/BUILD_NOTICE.txt" if prepared is not None
                            else "licenses/wgpu-native/NOTICE.txt")
        hashes[build_notice.as_posix()] = copy_file(webgpu_output / "NOTICE.txt", stage / build_notice)
        copy_tree(webgpu_output / "licenses", stage / "licenses/wgpu-dependencies", hashes, stage)
        if prepared is not None:
            # copy_tree records file hashes. Preserve directory modes as well
            # because the preparation receipt binds the full provenance tree.
            provenance_source = webgpu_output / "licenses/application-patches"
            provenance_destination = stage / "licenses/wgpu-dependencies/application-patches"
            for directory in sorted((path for path in provenance_source.rglob("*") if path.is_dir()), reverse=True):
                shutil.copystat(directory, provenance_destination / directory.relative_to(provenance_source))
            if tree_receipt(stage / "licenses/wgpu-dependencies/application-patches") != prepared["application_patch_provenance"]:
                raise ValueError("staged application patch provenance differs from preparation")
            for name, source, expected in (("PREPARED.json", webgpu_output / "PREPARED.json", prepared_hash),
                                          ("BUILD.json", build_manifest, build_hash)):
                destination = Path("provenance/webgpu") / name
                hashes[destination.as_posix()] = copy_file(source, stage / destination, expected)
        lavapipe_licenses = relocated_build_path(
            Path(link.get("licenses", "")), repository_root)
        copy_tree(lavapipe_licenses, stage / "licenses/software-vulkan",
                  hashes, stage)

        (stage / "link.args").write_text("".join(argument + "\n" for argument in staged_arguments))
        hashes["link.args"] = sha256(stage / "link.args")
        metadata = {
            "format": 1,
            "target": "x86_64 Cosmopolitan APE",
            "runtime": runtime,
            "wgpu_native": "v29.0.1.1",
            "mesa": link.get("mesa_version"),
            "llvm": link.get("llvm_version"),
            "provider": link.get("provider"),
            "capabilities": ["compute", "offscreen-rgba8-raster", "texture-readback"],
            "link_arguments": len(staged_arguments),
            "files": dict(sorted(hashes.items())),
        }
        # Preserve explicitly recorded validation separately from capabilities.
        # Older stock fixtures without BUILD.json make no validation claim.
        metadata["validation"] = {
            key: build[key] for key in ("build_status", "compile_only", "probes")
            if key in build
        }
        if selected_runtime(rust_sdk / "vendor/cosmocc") != runtime:
            raise ValueError("selected runtime changed while staging the WebGPU SDK")
        if prepared is not None:
            verify_prepared(webgpu_output, repository_root / "third_party/wgpu_native", prepared_hash)
            if file_sha256(build_manifest) != build_hash:
                raise ValueError("WebGPU BUILD.json changed while staging the SDK")
            metadata.update({
                "header_source": "prepared",
                "prepared_receipt_sha256": prepared_hash,
                "prepared_source_sha256": prepared["prepared_source_sha256"],
                "application_patches": prepared["application_patches"],
                "application_patch_order": prepared["application_patch_order"],
                "application_patch_licenses_path": "licenses/wgpu-dependencies/application-patches",
            })
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
    parser.add_argument("--build-manifest", type=Path,
                        help="WebGPU BUILD.json (default: o/webgpu/BUILD.json; required for source runtime)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = stage_webgpu_sdk(args.link_manifest, args.wgpu_archive,
                                args.rust_sdk, args.output,
                                build_manifest=args.build_manifest)
    print(f"WebGPU SDK staged: {args.output} ({len(metadata['files'])} hashed files)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as error:
        raise SystemExit(f"WebGPU SDK staging failed: {error}") from error
