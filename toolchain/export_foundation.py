#!/usr/bin/env python3
"""Export completed source-runtime inputs without building or running them.

Reads, copies and hashes an already completed x86-64 software foundation. Git
is used only to inspect committed inputs and stream a candidate source archive.
The output is new, isolated and relocatable; the application's pin is untouched.
Content receipts are not signed build attestations or runtime qualification.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from runtime_identity import (MANIFEST, canonical, digest_file, read_json,
                              verify_runtime_sdk, verify_source_inputs)
from source_runtime import (git, git_bytes, git_command, git_environment,
                            publish_new_directory, tracked_source_inventory)
from static_archives import archive_sha256, verify_archives
from webgpu_sdk import archive_destination, stage_webgpu_sdk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/rust_ape"))
sys.path.insert(0, str(ROOT / "third_party/wgpu_native"))
from runtime_profile import (BINDING, PROFILE, TABLES, expected_shim_objects,
                             verify_binding)
from source_receipt import verify_prepared
from build_receipt import verify_cargo

TOOLS = {
    "c_compiler": "x86_64-unknown-cosmo-cc", "cxx_compiler": "x86_64-unknown-cosmo-c++",
    "archiver": "x86_64-unknown-cosmo-ar", "ranlib": "x86_64-linux-cosmo-ranlib",
    "strip": "x86_64-linux-cosmo-strip", "ape_linker": "apelink", "ape_loader": "ape-x86_64.elf",
}
APPLICATION_PATCH = "backend/patches/0001-bounded-native-adapter-enumeration.patch"
LINK_OPTIONS = {
    "-Wl,--start-group", "-Wl,--end-group", "-Wl,--whole-archive", "-Wl,--no-whole-archive",
    "-lcxx", "-lrt", "-ldl", "-lm", "-lpthread",
}
RUNTIME_LINK_OPTIONS = ["-Wl,--defsym,__isoc23_strtol=strtol", "-Wl,--defsym,__isoc23_sscanf=sscanf"]


def canonical_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def relative_name(value):
    if (not isinstance(value, str) or not value or str(PurePosixPath(value)) != value
            or PurePosixPath(value).is_absolute()
            or any(part in (".", "..") for part in value.split("/"))
            or any(character in value for character in "\\:;\n\r\0$[]")
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"Unsafe export-relative path: {value!r}")
    return value


def regular_file(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError(f"Symlink outside the SDK alias inventory: {part}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Expected a regular input file: {path}")
    return path


def regular_files(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Expected an ordinary directory: {directory}")
    result = {}
    for path in sorted(directory.iterdir()):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            for name, child in regular_files(path).items():
                result[path.name + "/" + name] = child
        elif stat.S_ISREG(mode):
            result[path.name] = path
        else:
            raise ValueError(f"Symlink or special file in export/input tree: {path}")
    for name in result:
        relative_name(name)
    return result


def copy_checked(source, destination, expected=None, mode=None):
    source = regular_file(source)
    before = source.stat()
    expected = digest_file(source) if expected is None else expected
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Refusing to replace staged file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(stat.S_IMODE(before.st_mode) if mode is None else mode)
    after = source.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
    if (any(getattr(before, field) != getattr(after, field) for field in fields)
            or digest_file(destination) != expected):
        raise ValueError(f"Input changed or differs from producer receipt: {source}")
    return expected


def materialized_sdk_files(records):
    """Map exported names to (original regular target name, target record).

    Resolve each recorded link component before '..', never recursively follow
    host symlinks. Directory aliases are expanded with ancestor-cycle detection.
    """
    if not isinstance(records, dict) or not records:
        raise ValueError("SDK must have a complete file inventory")
    for name, entry in records.items():
        relative_name(name)
        if not isinstance(entry, dict) or entry.get("type") not in ("file", "directory", "symlink"):
            raise ValueError(f"Unsupported SDK entry: {name}")
        parts = name.split("/")
        for index in range(1, len(parts)):
            parent = records.get("/".join(parts[:index]))
            if parent is not None and parent.get("type") != "directory":
                raise ValueError(f"SDK inventory descends through a non-directory: {name}")

    def children(name):
        prefix = name + "/" if name else ""
        return sorted({path[len(prefix):].split("/")[0] for path in records
                       if path.startswith(prefix) and path != name})

    def resolve(name):
        pending = name.split("/") if name else []
        resolved = []
        followed = 0
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise ValueError("SDK alias escapes the SDK")
                resolved.pop()
                continue
            candidate = "/".join([*resolved, part])
            relative_name(candidate)
            entry = records.get(candidate)
            if entry is None and children(candidate):
                entry = {"type": "directory"}
            if entry is None:
                raise ValueError(f"Dangling SDK alias: {candidate}")
            if entry["type"] == "symlink":
                followed += 1
                if followed > 40:
                    raise ValueError(f"Cyclic or excessive SDK alias: {candidate}")
                target = entry.get("target")
                if (not isinstance(target, str) or not target or target.startswith("/")
                        or any(char in target for char in "\\\n\r\0")):
                    raise ValueError(f"Unsafe SDK alias target: {candidate}")
                if hashlib.sha256(target.encode()).hexdigest() != entry.get("sha256"):
                    raise ValueError(f"SDK alias digest mismatch: {candidate}")
                pending = target.split("/") + pending
                continue
            resolved.append(part)
            if entry["type"] == "file" and pending:
                raise ValueError(f"SDK alias traverses a regular file: {candidate}")
        name = "/".join(resolved)
        return name, records.get(name, {"type": "directory"})

    result = {}

    def expand(name, destination, ancestors):
        target, entry = resolve(name)
        if entry["type"] == "file":
            if (type(entry.get("mode")) is not int or type(entry.get("size")) is not int
                    or entry["size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))):
                raise ValueError(f"Invalid SDK file fingerprint: {target}")
            result[relative_name(destination)] = (target, entry)
            return
        if target in ancestors:
            raise ValueError(f"Cyclic SDK directory alias: {name}")
        for child in children(target):
            expand(posixpath.join(target, child), posixpath.join(destination, child), ancestors | {target})

    expand("", "", set())
    return result


def materialize_sdk(source, destination, record):
    for name, (target, entry) in materialized_sdk_files(record["files"]).items():
        path = source / target
        if path.stat().st_size != entry["size"]:
            raise ValueError(f"SDK target size changed: {target}")
        copy_checked(path, destination / name, entry["sha256"], entry["mode"])
    # Upstream identity stays byte-for-byte intact, despite expanded aliases.
    copy_checked(source / MANIFEST, destination / MANIFEST)
    verify_materialized_sdk(destination, record)


def verify_materialized_sdk(sdk, record):
    files = regular_files(sdk)
    expected = materialized_sdk_files(record["files"])
    if set(files) != set(expected) | {MANIFEST} or read_json(sdk / MANIFEST) != record:
        raise ValueError("Materialized SDK file closure differs from source receipt")
    for name, (_, entry) in expected.items():
        info = files[name].stat()
        if (info.st_size != entry["size"] or stat.S_IMODE(info.st_mode) != entry["mode"]
                or digest_file(files[name]) != entry["sha256"]):
            raise ValueError(f"Materialized SDK input differs: {name}")


def artifact_kind(path, *, sdk=False):
    with path.open("rb") as stream:
        magic = stream.read(8)
    if magic == b"!<thin>\n":
        raise ValueError(f"Thin archives cannot be exported: {path}")
    if path.suffix in (".a", ".rlib") or magic == b"!<arch>\n":
        if sdk and magic == b"!<arch>\n" and path.stat().st_size == 8:
            # package.sh supplies empty linker-name stubs: support data only.
            return "data"
        archive_sha256(path)
        return "archive"
    if path.suffix == ".o":
        if not path.stat().st_size:
            raise ValueError(f"Empty object input: {path}")
        return "object"
    return "executable" if path.stat().st_mode & stat.S_IXUSR else "data"


def write_json(path, value):
    if path.exists() or path.is_symlink():
        raise ValueError(f"Refusing to replace staged record: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True).encode("ascii") + b"\n")


def checked_link_option(value):
    if value not in LINK_OPTIONS and re.fullmatch(
            r"-Wl,--defsym,[A-Za-z_][A-Za-z0-9_]*=[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise ValueError(f"Unreviewed dependency link option: {value!r}")
    return value


def git_archive_sha256(root, revision):
    """Hash actual Git tar bytes using only committed archive attributes.

    A local bare clone prevents caller-local info/attributes and configuration
    from changing the pin. No checkout, clean/smudge filters, network fetch or
    hardlinked object store is used, and the caller's metadata is untouched.
    """
    root = Path(root).resolve(strict=True)
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("Foundation archive requires a full committed revision")
    with tempfile.TemporaryDirectory(prefix="foundation-archive-") as temporary:
        isolated = Path(temporary) / "source.git"
        git_bytes(Path(temporary), "clone", "--quiet", "--bare", "--no-checkout",
                  "--no-local", "--template=", "--", str(root), str(isolated))
        object_format = git(isolated, "rev-parse", "--show-object-format")
        if object_format not in ("sha1", "sha256"):
            raise ValueError("Unsupported foundation Git object format")
        # Keep only the format required to read this owned object database.
        # Clone-created remote settings and any repository-local overrides are
        # unnecessary for archive generation and cannot become archive inputs.
        config = "[core]\n\trepositoryformatversion = " + ("1" if object_format == "sha256" else "0")
        config += "\n\tbare = true\n"
        if object_format == "sha256":
            config += "[extensions]\n\tobjectFormat = sha256\n"
        (isolated / "config").write_text(config)
        if git(isolated, "rev-parse", "--verify", revision + "^{commit}") != revision:
            raise ValueError("Foundation archive revision does not identify the selected commit")
        with tempfile.TemporaryFile() as errors:
            command = git_command(isolated, "-c", "core.attributesFile=" + os.devnull,
                                  "-c", "tar.umask=0002", "archive", "--format=tar", revision, ".")
            process = subprocess.Popen(command, env=git_environment(),
                                       stdout=subprocess.PIPE, stderr=errors)
            try:
                checksum = hashlib.sha256()
                for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                    checksum.update(chunk)
                code = process.wait()
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            if code:
                errors.seek(0)
                raise ValueError("Cannot archive pinned foundation: " + errors.read().decode(errors="replace"))
        return checksum.hexdigest()


def verify_source(root, sdk, record):
    source = record["source"]
    # Status/diff may normalize attributes or execute clean filters. Source
    # identity is raw Git blob bytes, checked below against HEAD and the index.
    untracked = git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked or git(root, "remote", "get-url", "origin") != source["repository"]:
        raise ValueError("Exporter requires committed source, no nonignored untracked files, and the SDK's origin")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", source["repository"]):
        raise ValueError("Foundation origin must be an HTTPS repository URL without credentials")
    inputs = verify_source_inputs(sdk, record)
    if inputs is None:
        raise ValueError("Export requires SOURCE_INPUTS.json and complete source evidence")
    actual = tracked_source_inventory(root, source["revision"], source["tree"])
    if actual != inputs["files"]:
        raise ValueError("SDK source inventory differs from the selected committed checkout")
    return inputs


@contextmanager
def source_profile():
    previous = os.environ.get("COSMO_RUNTIME_PROFILE")
    os.environ["COSMO_RUNTIME_PROFILE"] = "source-built"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COSMO_RUNTIME_PROFILE", None)
        else:
            os.environ["COSMO_RUNTIME_PROFILE"] = previous


def runtime_summary(record):
    return {key: record[key] for key in ("kind", "source", "runtime_identity", "runtime_abi")}


def runtime_identity_sha256(data):
    runtime, toolchain = data["runtime"], data["toolchain"]
    names = {runtime["provenance_record"], *runtime["crt_objects"], *runtime["archives"],
             *runtime["cxx_abi_archives"], *toolchain["support_files"],
             *(toolchain[name] for name in TOOLS)}
    return canonical_hash({"foundation": data["foundation"], "target": data["target"],
                           "runtime": runtime, "toolchain": toolchain,
                           "artifacts": {name: data["artifacts"][name] for name in sorted(names)}})


def header_tree_sha256(directory):
    files = regular_files(directory)
    if not files:
        raise ValueError("Source SDK headers are empty")
    return canonical_hash({name: digest_file(path) for name, path in files.items()})


def verified_rust_target(build, invocation, rust_sdk):
    """Bind the HANDOFF label and preserved spec to the target Cargo verified."""
    environment = invocation.get("environment")
    selected = environment.get("CARGO_BUILD_TARGET") if isinstance(environment, dict) else None
    expected = rust_sdk / "generated/x86_64-unknown-linux-musl.json"
    if not isinstance(selected, str) or Path(selected) != expected:
        raise ValueError("Rust invocation target does not name the verified SDK target")
    target = regular_file(expected)
    checksum = digest_file(target)
    if (build.get("rust_target") != target.stem
            or invocation.get("target_sha256") != checksum
            or build.get("rust_target_spec_sha256") != checksum):
        raise ValueError("Rust target label/spec differs from the actual verified SDK target")
    return target.stem, target


def verified_c_target(llvm, llvm_pins):
    triple = llvm.get("target_triple")
    if (not isinstance(triple, str) or not re.fullmatch(r"x86_64-[a-z0-9_-]+", triple)
            or triple != llvm_pins.get("target_triple")):
        raise ValueError("LLVM target triple differs from the committed producer target")
    return triple


def verify_inputs(root, sdk, rust_sdk, wgpu_archive, mesa_path, llvm_path):
    """Require producer completion evidence, not caller-supplied identities."""
    record = verify_runtime_sdk(sdk)
    inputs = verify_source(root, sdk, record)
    runtime = runtime_summary(record)
    binding = verify_binding(rust_sdk)
    installed = verify_runtime_sdk(rust_sdk / "vendor/cosmocc")
    if (installed != record or binding.get("runtime_identity") != runtime["runtime_identity"]
            or binding.get("runtime_abi") != runtime["runtime_abi"]):
        raise ValueError("Rust SDK is not bound to the selected complete source SDK")
    shims = expected_shim_objects(rust_sdk)
    out = root / "o/webgpu"
    build_path = regular_file(out / "BUILD.json")
    mesa_path, llvm_path = regular_file(mesa_path), regular_file(llvm_path)
    build, mesa, llvm = (read_json(path) for path in (build_path, mesa_path, llvm_path))
    for label, value in (("WebGPU", build), ("Mesa", mesa), ("LLVM", llvm)):
        if not isinstance(value, dict) or value.get("runtime") != runtime:
            raise ValueError(f"{label} completed record selects a different runtime")
    for value in (mesa, llvm):
        selected_sdk = Path(value["sdk_root"])
        if not selected_sdk.is_absolute() or verify_runtime_sdk(selected_sdk) != record:
            raise ValueError("Dependency producer SDK differs from the selected source SDK")
        if value.get("static") is not True:
            raise ValueError("Completed dependencies must be static")
    if (mesa.get("external_vulkan_loader") is not False or mesa.get("provider") != "embedded"
            or mesa.get("llvm") != llvm or Path(mesa["llvm_metadata"]) != llvm_path
            or mesa.get("llvm_metadata_sha256") != digest_file(llvm_path)):
        raise ValueError("Mesa does not bind the exact current LLVM metadata")
    if (build.get("build_status") != "built" or build.get("embedded_software_vulkan") is not True
            or build.get("software_vulkan") != mesa or build.get("source_dirty") is not False
            or build.get("source_commit") != record["source"]["revision"]
            or build.get("target") != "x86_64 Cosmopolitan APE"
            or build.get("cosmocc") != "source-built"):
        raise ValueError("WebGPU requires a completed clean source-runtime software build")
    checksum = archive_sha256(regular_file(wgpu_archive))
    delivery = {"mode": "embedded-in-wgpu", "container_sha256": checksum}
    if build.get("wgpu_archive_sha256") != checksum or build.get("rust_std_delivery") != delivery:
        raise ValueError("WebGPU/Rust delivery receipt differs from the selected archive")
    if build.get("rust_shim_sha256") != {path.name: digest_file(path) for path in shims}:
        raise ValueError("WebGPU build does not bind the exact source-profile shim objects")
    prepared_hash = build.get("prepared_receipt_sha256")
    if not isinstance(prepared_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", prepared_hash):
        raise ValueError("Completed WebGPU build lacks its preparation receipt hash")
    prepared, _ = verify_prepared(out, root / "third_party/wgpu_native", prepared_hash)
    patches = prepared["application_patches"]
    if (APPLICATION_PATCH not in patches or build.get("application_patches") != patches
            or build.get("prepared_source_sha256") != prepared["prepared_source_sha256"]):
        raise ValueError("WebGPU is not the receipted application-patched prepared build")
    if (build.get("foundation_inputs") != prepared["foundation_inputs"]
            or build.get("application_patch_order") != prepared["application_patch_order"]
            or build.get("prepared_lock_sha256") != prepared["prepared_lock_sha256"]):
        raise ValueError("Completed WebGPU build differs from preparation inputs")
    if build.get("lock_sha256") != digest_file(root / "third_party/wgpu_native/Cargo.cosmo.lock"):
        raise ValueError("Completed WebGPU foundation lockfile changed")
    raw_records = {
        "webgpu/BUILD.json": build_path, "webgpu/PREPARED.json": out / "PREPARED.json",
        "mesa/LINK.json": mesa_path, "llvm/LINK.json": llvm_path,
        "rust/" + PROFILE: rust_sdk / PROFILE, "rust/" + BINDING: rust_sdk / BINDING,
        "webgpu/Cargo.lock": out / "source/Cargo.lock",
        "webgpu/Cargo.cosmo.lock": root / "third_party/wgpu_native/Cargo.cosmo.lock",
        "webgpu/cargo.cosmo.toml": out / "cargo.cosmo.toml",
    }
    for name in ("cargo-artifacts.jsonl", "cargo-invocation.json"):
        field = name.split(".")[0].replace("-", "_") + "_sha256"
        path = regular_file(out / name)
        if build.get(field) != digest_file(path):
            raise ValueError(f"WebGPU completed record does not bind {name}")
        raw_records["webgpu/" + name] = path
    cargo = verify_cargo(out, rust_sdk, runtime, wgpu_archive,
                         expected_invocation_sha256=build["cargo_invocation_sha256"],
                         expected_artifacts_sha256=build["cargo_artifacts_sha256"])
    invocation = cargo["cargo_invocation"]
    recipe = build.get("build_recipe")
    if (not isinstance(recipe, dict) or recipe.get("cargo") != invocation
            or invocation.get("runtime") != runtime or invocation.get("schema_version") != 1
            or invocation.get("status") != "invocation-only" or invocation.get("rust") != build.get("rust")):
        raise ValueError("WebGPU Cargo invocation and build recipe are not bound")
    commands_path = regular_file(out / "build-commands.json")
    if (recipe.get("commands_sha256") != digest_file(commands_path)
            or recipe.get("commands") != read_json(commands_path)):
        raise ValueError("WebGPU link recipe changed")
    for field, name in (("build_script_sha256", "third_party/wgpu_native/build.sh"),
                        ("receipt_helper_sha256", "third_party/wgpu_native/build_receipt.py")):
        if recipe.get(field) != digest_file(regular_file(root / name)):
            raise ValueError("WebGPU recipe source differs from the selected foundation")
    if invocation.get("wrapper_sha256") != digest_file(root / "third_party/rust_ape/run.sh"):
        raise ValueError("Cargo wrapper differs from the selected foundation")
    configs = invocation.get("config_sha256")
    if not isinstance(configs, dict) or not configs:
        raise ValueError("Cargo invocation lacks its effective configurations")
    for name, expected in configs.items():
        if not Path(name).is_absolute() or digest_file(regular_file(name)) != expected:
            raise ValueError("Cargo invocation configuration changed")
    rust_triple, rust_target = verified_rust_target(build, invocation, rust_sdk)
    raw_records["webgpu/build-commands.json"] = commands_path
    raw_records["rust/target.json"] = rust_target
    for index, name in enumerate(sorted(configs)):
        raw_records[f"rust/cargo-configs/{index}.toml"] = regular_file(name)
    for name in TABLES:
        raw_records["rust/" + name] = regular_file(rust_sdk / name)
    for name, path in regular_files(out / "application-patches").items():
        raw_records["webgpu/application-patches/" + name] = path
    llvm_archives = llvm.get("archives")
    if not isinstance(llvm_archives, list) or not llvm_archives or len(set(llvm_archives)) != len(llvm_archives):
        raise ValueError("LLVM archive closure is missing or duplicated")
    verify_archives([regular_file(path) for path in llvm_archives], llvm.get("archive_sha256"))
    arguments = mesa.get("link_args")
    if not isinstance(arguments, list) or not arguments or any(not isinstance(arg, str) or not arg for arg in arguments):
        raise ValueError("Mesa link arguments are missing")
    for argument in arguments:
        if argument.startswith("-"):
            checked_link_option(argument)
        elif not Path(argument).is_absolute():
            raise ValueError("Producer dependency archive path must be absolute")
    archives = [arg for arg in arguments if not arg.startswith("-")]
    expected_archives = {mesa.get("registration_archive"), mesa.get("archive"), *llvm_archives}
    if set(archives) != expected_archives:
        raise ValueError("Mesa link arguments do not describe exactly the Mesa/LLVM closure")
    verify_archives([regular_file(path) for path in archives], mesa.get("link_archive_sha256"))
    for field in ("archive", "registration_archive"):
        if mesa.get(field + "_sha256") != mesa["link_archive_sha256"].get(mesa[field]):
            raise ValueError("Mesa primary archive identity differs from its link closure")
    wgpu_pins = read_json(root / "third_party/wgpu_native/IMPORT.json")
    llvm_pins = read_json(root / "third_party/llvm_cosmo/IMPORT.json")
    c_triple = verified_c_target(llvm, llvm_pins)
    mesa_pins = read_json(root / "third_party/lavapipe/PIN.json")
    if (wgpu_pins["wgpu_native_release"] != build.get("wgpu_native")
            or llvm_pins["llvm_version"] != llvm.get("llvm_version")
            or llvm_pins["sources"] != llvm.get("sources")
            or mesa_pins["mesa"]["version"] != mesa.get("mesa_version")
            or {key: mesa_pins[key] for key in ("mesa", "glslang")} != mesa.get("sources")):
        raise ValueError("Completed dependency versions/sources differ from the committed producer pins")
    for component, producer in (("llvm_cosmo", llvm), ("lavapipe", mesa)):
        directory = root / "third_party" / component
        patches = {path.name: digest_file(regular_file(path)) for path in sorted((directory / "patches").glob("*.patch"))}
        if (producer.get("recipe_sha256") != digest_file(directory / "build.py") or producer.get("patches") != patches):
            raise ValueError("Dependency recipe/patch identity differs from the selected foundation")
    versions = (build.get("wgpu_native"), build.get("rust"), mesa.get("mesa_version"), llvm.get("llvm_version"))
    if any(not isinstance(value, str) or not value.strip() for value in versions):
        raise ValueError("Completed dependency versions are missing")
    if mesa.get("llvm_version") != llvm["llvm_version"] or llvm.get("cosmocc_version") != "source-built":
        raise ValueError("Completed dependency versions/runtime labels are inconsistent")
    extra = {str(path): digest_file(regular_file(path)) for path in raw_records.values()}
    for directory in (out / "licenses", Path(mesa["licenses"])):
        files = regular_files(directory)
        if not files:
            raise ValueError("Completed dependency license tree is empty")
        extra.update({str(path): digest_file(path) for path in files.values()})
    extra[str(out / "NOTICE.txt")] = digest_file(regular_file(out / "NOTICE.txt"))
    return {"record": record, "inputs": inputs, "runtime": runtime, "binding": binding,
            "build": build, "mesa": mesa, "llvm": llvm, "prepared": prepared,
            "shims": shims, "raw_records": raw_records, "extra": extra,
            "wgpu_sha256": checksum, "c_triple": c_triple, "rust_triple": rust_triple}


def candidate_pin(root, verified):
    build, mesa, llvm = (verified[key] for key in ("build", "mesa", "llvm"))
    source = verified["record"]["source"]
    return {"format": 1, "repository": source["repository"], "revision": source["revision"],
            "paths": ["."], "archive_sha256": git_archive_sha256(root, source["revision"]),
            "wgpu_native_version": build["wgpu_native"].removeprefix("v"),
            "mesa_version": mesa["mesa_version"], "llvm_version": llvm["llvm_version"],
            "cosmocc_version": "source-built", "rust_toolchain": build["rust"]}


def dependency_path(source):
    return "dependencies/" + archive_destination(Path(source)).as_posix()


def assemble_handoff(stage, verified, pin):
    record, runtime = verified["record"], verified["runtime"]
    build, mesa, llvm = (verified[key] for key in ("build", "mesa", "llvm"))
    wgpu = "dependencies/lib/wgpu/libwgpu_native.a"
    objects = ["dependencies/lib/rust-ape/" + path.name for path in verified["shims"]]
    archives = {
        "rust_std": [wgpu], "webgpu": [wgpu],
        "mesa": [dependency_path(mesa[name]) for name in ("registration_archive", "archive")],
        "llvm": [dependency_path(name) for name in llvm["archives"]],
    }
    for name, source in verified["raw_records"].items():
        copy_checked(source, stage / "provenance" / name, verified["extra"][str(source)])
    source = record["source"]
    versions = {"rust_std": build["rust"], "webgpu": build["wgpu_native"],
                "mesa": mesa["mesa_version"], "llvm": llvm["llvm_version"]}
    provenance = {}
    for component in versions:
        name = f"provenance/components/{component}.json"
        provenance[component] = name
        evidence = {"schema_version": 1, "runtime": runtime,
                    "verification": "static-identity-only", "producer_records": {}}
        prefixes = {"rust_std": ("rust/", "webgpu/"), "webgpu": ("webgpu/",),
                    "mesa": ("mesa/",), "llvm": ("llvm/",)}[component]
        for original in verified["raw_records"]:
            if original.startswith(prefixes):
                path = "provenance/" + original
                evidence["producer_records"][path] = digest_file(stage / path)
        if component == "rust_std":
            evidence["archive_delivery"] = build["rust_std_delivery"]
        if component == "webgpu":
            evidence["application_patches"] = build["application_patches"]
            evidence["prepared_source_sha256"] = build["prepared_source_sha256"]
        write_json(stage / name, evidence)
    write_json(stage / "PIN.candidate.json", pin)
    # Inventory before LINK to avoid self-referential hashes.
    artifacts = {}
    for name, path in regular_files(stage).items():
        kind = artifact_kind(path, sdk=name.startswith("sdk/"))
        if name.startswith("provenance/") or path.suffix in (".json", ".jsonl"):
            kind = "data"
        artifacts[name] = {"sha256": digest_file(path), "kind": kind}
    link = {
        "format": 1, "path_mode": "export-root-relative", "pin": pin,
        "sdk_root": "sdk", "runtime": runtime, "target": "x86_64 Cosmopolitan APE",
        "static": True, "runtime_shared_libraries": [],
        "application_patches": build["application_patches"],
        "wgpu_archive": wgpu, "rust_std_linkage": "embedded-in-wgpu",
        "runtime_objects": objects, "runtime_link_options": RUNTIME_LINK_OPTIONS,
        "runtime_sources": ["dependencies/runtime/" + name for name in ("vulkan_loader.c", "win64_bridge.c")],
        "include_dirs": ["dependencies/include", "dependencies/runtime"],
        "driver_link_args": [checked_link_option(arg) if arg.startswith("-") else dependency_path(arg)
                             for arg in mesa["link_args"]],
        "software_vulkan": mesa, "licenses": "dependencies/licenses",
        "license_sha256": {name: digest_file(path) for name, path in regular_files(stage / "dependencies/licenses").items()},
        "file_sha256": {name: entry["sha256"] for name, entry in artifacts.items() if not name.startswith("sdk/")},
    }
    write_json(stage / "dependencies/LINK.json", link)
    artifacts["dependencies/LINK.json"] = {"sha256": digest_file(stage / "dependencies/LINK.json"), "kind": "data"}
    tools = {field: "sdk/bin/" + filename for field, filename in TOOLS.items()}
    runtime_paths = {"crt_objects": ["sdk/x86_64-linux-cosmo/lib/crt.o"],
                     "archives": ["sdk/x86_64-linux-cosmo/lib/libcosmo.a"],
                     "cxx_abi_archives": ["sdk/x86_64-linux-cosmo/lib/libcxx.a"]}
    for name in tools.values():
        if artifacts.get(name, {}).get("kind") != "executable":
            raise ValueError(f"Required SDK entry point is not executable: {name}")
    for field, names in runtime_paths.items():
        for name in names:
            if artifacts.get(name, {}).get("kind") != ("object" if field == "crt_objects" else "archive"):
                raise ValueError(f"Required runtime input has the wrong artifact kind: {name}")
    direct = {*tools.values(), "sdk/" + MANIFEST, *(name for names in runtime_paths.values() for name in names)}
    tools["support_files"] = sorted(name for name in artifacts if name.startswith("sdk/") and name not in direct)
    handoff = {
        "format": "easy-diffusion-foundation-handoff", "schema_version": 1,
        "foundation": {"repository": source["repository"], "revision": source["revision"],
                       "source_tree_sha256": record["source_tree_sha256"], "clean": True},
        "target": {"architecture": "x86_64", "abi": "cosmopolitan-ape",
                   "c_triple": verified["c_triple"], "rust_triple": verified["rust_triple"]},
        "runtime": {"kind": "source-built", "provenance": {
            "repository": source["repository"], "revision": source["revision"],
            "patches_sha256": canonical_hash(record["source_patches"])},
            "provenance_record": "sdk/" + MANIFEST,
            "headers": {"path": "sdk/include", "tree_sha256": header_tree_sha256(stage / "sdk/include")},
            **runtime_paths},
        "toolchain": tools, "artifacts": dict(sorted(artifacts.items())), "components": {},
    }
    identity = runtime_identity_sha256(handoff)
    handoff["runtime_identity_sha256"] = identity
    for name, version in versions.items():
        handoff["components"][name] = {
            "version": version,
            "target_triple": verified["rust_triple"] if name in ("rust_std", "webgpu") else verified["c_triple"],
            "runtime_identity_sha256": identity, "archives": archives[name],
            "support_objects": objects if name == "rust_std" else [], "provenance": provenance[name],
        }
    write_json(stage / "HANDOFF.json", handoff)
    return handoff


def verify_export(stage, handoff, record):
    """Rehash the entire staged closure before publication, without execution."""
    actual = regular_files(stage)
    if set(actual) != set(handoff["artifacts"]) | {"HANDOFF.json"}:
        raise ValueError("Staged export closure has missing or unexpected files")
    if read_json(stage / "HANDOFF.json") != handoff:
        raise ValueError("Staged handoff changed")
    for name, entry in handoff["artifacts"].items():
        path = actual[name]
        if digest_file(path) != entry["sha256"]:
            raise ValueError(f"Staged artifact changed: {name}")
        if entry["kind"] == "archive":
            archive_sha256(path)
        if entry["kind"] == "executable" and not path.stat().st_mode & stat.S_IXUSR:
            raise ValueError(f"Staged executable lost its mode: {name}")
    verify_materialized_sdk(stage / "sdk", record)
    if (runtime_identity_sha256(handoff) != handoff["runtime_identity_sha256"]
            or header_tree_sha256(stage / "sdk/include") != handoff["runtime"]["headers"]["tree_sha256"]):
        raise ValueError("Staged runtime/header identity changed")
    for component in handoff["components"].values():
        for field, kind in (("archives", "archive"), ("support_objects", "object")):
            for name in component[field]:
                if handoff["artifacts"].get(name, {}).get("kind") != kind:
                    raise ValueError(f"Component artifact kind differs: {name}")


def export_foundation(repository_root, source_sdk, rust_sdk, output, *, wgpu_archive=None,
                      mesa_manifest=None, llvm_manifest=None):
    root, sdk, rust_sdk = (Path(value).absolute() for value in (repository_root, source_sdk, rust_sdk))
    for directory in (root, sdk, rust_sdk):
        if directory.is_symlink() or directory.resolve(strict=True) != directory:
            raise ValueError(f"Inputs require canonical ordinary directories: {directory}")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"Refusing pre-existing export output: {output}")
    for directory in (root, sdk, rust_sdk):
        if directory == output or directory in output.parents or output in directory.parents:
            raise ValueError("Export must be isolated outside its source and SDK input trees")
    if not output.parent.is_dir() or output.parent.resolve(strict=True) != output.parent:
        raise ValueError("Export parent must already exist and contain no directory aliases")
    wgpu = Path(wgpu_archive) if wgpu_archive is not None else root / "o/rust-ape/build/x86_64-unknown-linux-musl/release/libwgpu_native.a"
    mesa = Path(mesa_manifest) if mesa_manifest is not None else root / "o/lavapipe/LINK.json"
    llvm = Path(llvm_manifest) if llvm_manifest is not None else root / "o/llvm-cosmo/LINK.json"
    with source_profile():
        verified = verify_inputs(root, sdk, rust_sdk, wgpu, mesa, llvm)
        pin = candidate_pin(root, verified)
        with tempfile.TemporaryDirectory(prefix=".foundation-export-", dir=output.parent) as temporary:
            stage = Path(temporary) / "export"
            stage.mkdir()
            materialize_sdk(sdk, stage / "sdk", verified["record"])
            staged = stage_webgpu_sdk(mesa, wgpu, rust_sdk, stage / "dependencies", root,
                                      build_manifest=root / "o/webgpu/BUILD.json")
            for name, expected in staged["files"].items():
                if digest_file(stage / "dependencies" / name) != expected:
                    raise ValueError(f"Staged software dependency changed: {name}")
            handoff = assemble_handoff(stage, verified, pin)
            verify_export(stage, handoff, verified["record"])
            if verify_inputs(root, sdk, rust_sdk, wgpu, mesa, llvm) != verified:
                raise ValueError("Source or completed producer inputs changed during export")
            if digest_file(stage / "sdk" / MANIFEST) != digest_file(sdk / MANIFEST):
                raise ValueError("Export must preserve original SOURCE_RUNTIME.json bytes")
            if output.exists() or output.is_symlink():
                raise ValueError("Export destination appeared during staging; refusing replacement")
            publish_new_directory(stage, output)
    return handoff


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--source-sdk", type=Path, required=True)
    parser.add_argument("--rust-sdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wgpu-archive", type=Path)
    parser.add_argument("--mesa-manifest", type=Path)
    parser.add_argument("--llvm-manifest", type=Path)
    args = parser.parse_args()
    export_foundation(args.source_root, args.source_sdk, args.rust_sdk, args.output,
                      wgpu_archive=args.wgpu_archive, mesa_manifest=args.mesa_manifest,
                      llvm_manifest=args.llvm_manifest)
    print(f"Exported static source foundation: {args.output / 'HANDOFF.json'}")
    print("PIN.candidate.json requires separate review and application activation; no build or runtime test was run.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Foundation export failed: {error}") from error
