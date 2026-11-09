#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build an x86-64 Cosmopolitan LLVM MCJIT and a portable runtime probe."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "toolchain"))
from runtime_identity import selected_runtime
from static_archives import archive_hashes, verify_archives
PIN = json.loads((HERE / "IMPORT.json").read_text())


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def write_if_changed(path: Path, text: str) -> None:
    if not path.is_file() or path.read_text() != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text)
        temporary.replace(path)


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+ " + shlex.join(map(str, args)), flush=True)
    return subprocess.run(args, check=True, text=True, **kwargs)


def capture(args: list[str]) -> str:
    return subprocess.check_output(args, text=True).strip()


def tool(env: str, name: str) -> str:
    selected = os.environ.get(env, name)
    path = shutil.which(selected)
    if not path:
        raise RuntimeError(f"{name} is required on the build host (or set {env})")
    return str(Path(path).absolute())


def prepare(out: Path) -> Path:
    patches = sorted((HERE / "patches").glob("*.patch"))
    source_fingerprint = fingerprint({
        "sources": PIN["sources"],
        "patches": {p.name: sha(p) for p in patches},
    })
    stamp = out / "source.fingerprint"
    source = out / PIN["sources"][0]["directory"]
    # A cache hit must preserve all source mtimes to keep Ninja incremental.
    if (stamp.is_file() and stamp.read_text().strip() == source_fingerprint
            and (source / "CMakeLists.txt").is_file()
            and (out / "cmake/Modules/LLVMVersion.cmake").is_file()):
        return source
    downloads = out / "downloads"
    downloads.mkdir(exist_ok=True)
    for entry in PIN["sources"]:
        archive = downloads / entry["filename"]
        if not archive.is_file() or sha(archive) != entry["sha256"]:
            temporary = archive.with_suffix(archive.suffix + ".part")
            print(f"Downloading {entry['url']}", flush=True)
            with urllib.request.urlopen(entry["url"], timeout=120) as response, temporary.open("wb") as destination:
                shutil.copyfileobj(response, destination)
            if sha(temporary) != entry["sha256"]:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"Source SHA256 mismatch: {entry['filename']}")
            temporary.replace(archive)
        destination = out / entry["directory"]
        if destination.is_symlink():
            raise RuntimeError(f"Refusing to replace symlinked source: {destination}")
        if destination.exists():
            shutil.rmtree(destination)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if Path(member.name).parts[0] != entry["directory"]:
                    raise RuntimeError(f"Unexpected archive root: {member.name}")
            tar.extractall(out, filter="data")
    cmake_link = out / "cmake"
    wanted = PIN["sources"][1]["directory"]
    if cmake_link.is_symlink():
        if os.readlink(cmake_link) != wanted:
            cmake_link.unlink()
    elif cmake_link.exists():
        raise RuntimeError(f"Expected CMake source symlink: {cmake_link}")
    if not cmake_link.exists():
        cmake_link.symlink_to(wanted, target_is_directory=True)
    for patch in patches:
        run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)], cwd=source)
    stamp.write_text(source_fingerprint + "\n")
    return source


def configure(cmake: str, args: list[str], directory: Path, identity: object) -> None:
    stamp = directory / "configuration.fingerprint"
    value = fingerprint(identity)
    if (stamp.is_file() and stamp.read_text().strip() == value
            and (directory / "build.ninja").is_file()):
        return
    # Clear stale compiler-detection state; unchanged object commands still reuse
    # Ninja's existing .o files. An SDK change is included in this fingerprint.
    run([cmake, "--fresh", *args])
    stamp.write_text(value + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="fetch and patch sources without compiling")
    parser.add_argument("--no-run", action="store_true", help="build the probe but do not run it on this host")
    args = parser.parse_args()
    out = Path(os.environ.get("LLVM_COSMO_OUT", REPO / "o/llvm-cosmo")).absolute()
    out.mkdir(parents=True, exist_ok=True)
    if out in (Path("/"), REPO, HERE):
        raise RuntimeError("LLVM_COSMO_OUT must name a separate build directory")
    # Two Ninja writers can corrupt dependency metadata even for identical
    # object commands. Refuse overlapping builds of this output directory.
    lock_stream = (out / "build.lock").open("a")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError(f"Another LLVM build is using {out}") from error
    source = prepare(out)
    if args.prepare_only:
        print(source)
        return
    sdk = Path(os.environ.get("COSMOCC", REPO / "o/rust-ape/sdk/vendor/cosmocc")).absolute()
    if not (sdk / "bin/x86_64-unknown-cosmo-c++").is_file():
        raise RuntimeError("Selected Cosmopolitan SDK missing; run bash third_party/rust_ape/run.sh setup")
    runtime = selected_runtime(sdk)
    jobs = int(os.environ.get("LLVM_COSMO_JOBS", "2"))
    if jobs < 1:
        raise RuntimeError("LLVM_COSMO_JOBS must be positive")
    cmake = tool("CMAKE", "cmake")
    ninja = tool("NINJA", "ninja")
    host_cc = tool("LLVM_HOST_CC", "cc")
    host_cxx = tool("LLVM_HOST_CXX", "c++")
    common = [
        "-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM={ninja}",
        "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CXX_STANDARD=20",
        "-DCMAKE_C_FLAGS_RELEASE=-O1 -DNDEBUG", "-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG",
        "-DLLVM_TARGETS_TO_BUILD=X86", "-DLLVM_ENABLE_PROJECTS=",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF", "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_BUILD_TOOLS=OFF",
        "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF",
        "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_LIBEDIT=OFF",
        f"-DLLVM_PARALLEL_COMPILE_JOBS={jobs}", "-DLLVM_PARALLEL_LINK_JOBS=1",
    ]
    tools_identity = {"cmake": capture([cmake, "--version"]).splitlines()[0],
                      "ninja": capture([ninja, "--version"])}
    host = out / "host-build"
    host_args = ["-S", str(source), "-B", str(host), *common,
                 f"-DCMAKE_C_COMPILER={host_cc}", f"-DCMAKE_CXX_COMPILER={host_cxx}"]
    configure(cmake, host_args, host, {"args": host_args, "tools": tools_identity})
    # Sequential stages avoid nested Ninja builds exceeding the selected limit.
    run([ninja, "-C", str(host), f"-j{jobs}", "llvm-tblgen"])
    toolchain = out / "cosmopolitan.cmake"
    # CMake quoted strings: normalize paths and reject characters with semantics.
    if any(ch in str(sdk) for ch in ('"', ';', '\\', '\n', '$')):
        raise RuntimeError("COSMOCC path contains unsupported CMake characters")
    write_if_changed(toolchain, f'''set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(COSMO_SDK "{sdk}")
set(CMAKE_C_COMPILER "${{COSMO_SDK}}/bin/x86_64-unknown-cosmo-cc")
set(CMAKE_CXX_COMPILER "${{COSMO_SDK}}/bin/x86_64-unknown-cosmo-c++")
set(CMAKE_AR "${{COSMO_SDK}}/bin/x86_64-unknown-cosmo-ar")
set(CMAKE_RANLIB "${{COSMO_SDK}}/bin/x86_64-linux-cosmo-ranlib")
set(CMAKE_CROSSCOMPILING_EMULATOR "${{COSMO_SDK}}/bin/ape-x86_64.elf")
set(CMAKE_C_FLAGS_INIT "-ffunction-sections -fdata-sections")
set(CMAKE_CXX_FLAGS_INIT "-ffunction-sections -fdata-sections")
set(CMAKE_FIND_ROOT_PATH "${{COSMO_SDK}}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
''')
    target = out / "build"
    target_args = ["-S", str(source), "-B", str(target), *common,
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}", f"-DCMAKE_INSTALL_PREFIX={out / 'install'}",
        "-DCMAKE_SKIP_RPATH=ON", f"-DLLVM_TABLEGEN={host / 'bin/llvm-tblgen'}",
        f"-DLLVM_HOST_TRIPLE={PIN['target_triple']}", f"-DLLVM_DEFAULT_TARGET_TRIPLE={PIN['target_triple']}",
        "-DBUILD_SHARED_LIBS=OFF", "-DLLVM_BUILD_LLVM_DYLIB=OFF", "-DLLVM_LINK_LLVM_DYLIB=OFF",
        "-DLLVM_BUILD_STATIC=ON", "-DLLVM_INCLUDE_TOOLS=ON", "-DLLVM_ENABLE_PIC=OFF",
        "-DLLVM_ENABLE_EH=OFF", "-DLLVM_ENABLE_RTTI=OFF", "-DLLVM_ENABLE_LTO=OFF",
        "-DLLVM_ENABLE_MODULES=OFF", "-DLLVM_ENABLE_PEDANTIC=OFF", "-DLLVM_ENABLE_FFI=OFF",
        "-DLLVM_ENABLE_LIBPFM=OFF", "-DLLVM_ENABLE_CURL=OFF", "-DLLVM_ENABLE_THREADS=ON",
        "-DLLVM_ENABLE_UNWIND_TABLES=OFF", "-DHAVE_DLOPEN=0", "-DHAVE_DLADDR=0"]
    sdk_identity = {name: sha(sdk / name) for name in [
        "bin/x86_64-unknown-cosmo-cc", "bin/x86_64-unknown-cosmo-c++",
        "bin/x86_64-linux-cosmo-gcc", "bin/x86_64-linux-cosmo-g++",
        "libexec/gcc/x86_64-linux-cosmo/14.1.0/cc1",
        "libexec/gcc/x86_64-linux-cosmo/14.1.0/cc1plus",
        "x86_64-linux-cosmo/lib/libcosmo.a", "x86_64-linux-cosmo/lib/libcxx.a"]}
    sdk_identity["runtime"] = runtime
    sdk_stamp = out / "target-sdk.fingerprint"
    sdk_fingerprint = fingerprint(sdk_identity)
    if sdk_stamp.is_file() and sdk_stamp.read_text().strip() != sdk_fingerprint:
        # Ninja does not treat the compiler binary itself as an object input.
        # A changed compiler/runtime therefore needs a clean target build.
        if target.is_symlink():
            raise RuntimeError(f"Refusing to replace symlinked target build: {target}")
        if target.exists():
            shutil.rmtree(target)
    sdk_stamp.write_text(sdk_fingerprint + "\n")
    configure(cmake, target_args, target, {"args": target_args,
        "toolchain": sha(toolchain), "sdk": sdk_identity, "tools": tools_identity})
    targets = ["llvm-config", "LLVMBitWriter", "LLVMMCDisassembler", "LLVMMCJIT", "LLVMCore",
        "LLVMExecutionEngine", "LLVMScalarOpts", "LLVMTransformUtils", "LLVMInstCombine",
        "LLVMX86CodeGen", "LLVMX86AsmParser", "LLVMX86Disassembler", "LLVMX86TargetMCA",
        "LLVMCoroutines", "LLVMLTO", "LLVMIRReader", "LLVMInterpreter"]
    run([ninja, "-C", str(target), f"-j{jobs}", *targets])
    package(out, source, sdk, target, args.no_run, cmake, ninja, runtime)


def package(out: Path, source: Path, sdk: Path, target: Path, no_run: bool,
            cmake: str, ninja: str, runtime: dict) -> None:
    loader = sdk / "bin/ape-x86_64.elf"
    llvm_config = target / "bin/llvm-config"
    query = [str(loader), str(llvm_config)]
    components = PIN["components"]
    archives = shlex.split(capture([*query, "--link-static", "--libfiles", *components]))
    for name in archives:
        path = Path(name)
        if path.suffix != ".a" or not path.is_file() or path.stat().st_size < 8:
            raise RuntimeError(f"Expected completed static LLVM archive: {path}")
    archive_identity = archive_hashes(archives)
    system = shlex.split(capture([*query, "--link-static", "--system-libs", *components]))
    link_args = ["-Wl,--start-group", *archives, "-Wl,--end-group", "-lcxx", *system]
    wrapper = out / "bin/llvm-config"
    write_if_changed(wrapper, "#!/bin/sh\nexec " + shlex.join(query) + ' "$@"\n')
    wrapper.chmod(0o755)
    probe_object = out / "jit_probe.o"
    run([str(sdk / "bin/x86_64-unknown-cosmo-cc"), "-O1", "-g", "-fno-omit-frame-pointer",
         "-I" + str(source / "include"), "-I" + str(target / "include"),
         "-c", str(HERE / "jit_probe.c"), "-o", str(probe_object)])
    # Link into a fresh path, validate those bytes, then publish atomically.
    # This prevents a stale packed .exe from surviving a repeated SDK link.
    probe_directory = Path(tempfile.mkdtemp(prefix="probe-build-", dir=out))
    probe_destination = out / "llvm_jit_probe.exe"
    probe = probe_directory / "llvm_jit_probe.exe"
    run([str(sdk / "bin/x86_64-unknown-cosmo-c++"), str(probe_object), *link_args, "-o", str(probe)])
    # Use the same guarded PE stack setup as the WebGPU executable before hashing.
    run([sys.executable, str(REPO / "third_party/wgpu_native/configure_pe.py"), str(probe)])
    shutil.copy2(loader, out / "ape-x86_64.elf")
    (out / "ape-x86_64.elf").chmod(0o755)
    debug_file = out / "llvm_jit_probe.com.dbg"
    Path(str(probe) + ".dbg").replace(debug_file)
    before = sha(probe)
    if not no_run:
        try:
            result = subprocess.run([str(loader), str(probe)], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=180)
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output += "\nLLVM JIT probe timed out after 180 seconds.\n"
            print(output, end="", flush=True)
            (out / "jit-probe.log").write_text(output)
            raise RuntimeError("LLVM JIT probe timed out after 180 seconds") from error
        print(result.stdout, end="", flush=True)
        (out / "jit-probe.log").write_text(result.stdout)
        result.check_returncode()
        if sha(probe) != before:
            raise RuntimeError("The JIT probe modified its own executable")
    probe.replace(probe_destination)
    probe = probe_destination
    os.utime(probe, None)
    if sha(probe) != before:
        raise RuntimeError("Published JIT executable differs from validated bytes")
    shutil.rmtree(probe_directory)
    licenses = out / "licenses/llvm-19.1.7"
    for name in ["LICENSE.TXT", "include/llvm/Support/LICENSE.TXT",
                 "lib/Support/BLAKE3/LICENSE", "lib/Support/COPYRIGHT.regex"]:
        destination = licenses / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
    # These Support sources carry third-party terms in their leading comment.
    for name in ["lib/Support/xxhash.cpp", "include/llvm/Support/xxhash.h",
                 "lib/Support/MD5.cpp", "lib/Support/ConvertUTF.cpp"]:
        text = (source / name).read_text()
        blocks = re.findall(r"/\*.*?\*/", text.split("#include", 1)[0], re.S)
        if not blocks:
            raise RuntimeError(f"Missing expected inline license block: {name}")
        destination = licenses / (name + ".notice")
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_if_changed(destination, "\n\n".join(blocks) + "\n")
    for name in ("LICENSE.MIT", "LICENSE.superconfigure", "IMPORT.json"):
        shutil.copy2(HERE / name, out / "licenses" / name)
    verify_archives(archives, archive_identity)
    if selected_runtime(sdk) != runtime:
        raise RuntimeError("The selected runtime changed during the LLVM build")
    manifest = {"format": 1, "llvm_version": PIN["llvm_version"],
        "runtime": runtime,
        "cosmocc_version": (PIN["cosmocc_version"] if runtime["kind"] == "stock-sdk" else "source-built"), "target_triple": PIN["target_triple"],
        "sdk_root": str(sdk), "sources": PIN["sources"],
        "patches": {p.name: sha(p) for p in sorted((HERE / "patches").glob("*.patch"))},
        "source_fingerprint": (out / "source.fingerprint").read_text().strip(),
        "recipe_sha256": sha(HERE / "build.py"),
        "llvm_source_sha256": PIN["sources"][0]["sha256"],
        "host_tools": {"cmake": cmake, "ninja": ninja},
        "cpp_std": "c++20", "cpp_args": ["-fno-rtti", "-fno-exceptions"],
        "static": True, "rtti": False, "exceptions": False, "threads": True,
        "components": components, "archives": archives, "archive_sha256": archive_identity, "link_args": link_args,
        "include_dirs": [str(source / "include"), str(target / "include")],
        "llvm_config": str(wrapper), "probe": str(probe), "probe_sha256": before,
        "probe_debug": str(debug_file),
        "loader": str(out / "ape-x86_64.elf"), "licenses": str(out / "licenses"),
        "probe_run_on_build_host": not no_run}
    write_if_changed(out / "LINK.json", json.dumps(manifest, indent=2) + "\n")
    print(f"LLVM static link manifest: {out / 'LINK.json'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Cosmopolitan LLVM build failed: {error}") from error
