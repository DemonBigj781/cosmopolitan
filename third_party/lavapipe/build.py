#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a static Cosmopolitan lavapipe and its direct CPU-device probe."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
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
from static_archives import archive_hashes as static_archive_hashes, verify_archives
PIN = json.loads((HERE / "PIN.json").read_text())
MESA_TARGET = "src/gallium/targets/lavapipe/libvulkan_lvp.a"
PROBE_PASS = "PASS: direct static lavapipe instance and CPU enumeration"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def write_if_changed(path: Path, contents: str) -> None:
    if path.is_file() and path.read_text() == contents:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(contents)
    temporary.replace(path)


def matched(stamp: Path, value: str, required: list[Path]) -> bool:
    return (stamp.is_file() and stamp.read_text().strip() == value
            and all(path.is_file() for path in required))


def remove_directory(path: Path, out: Path) -> None:
    # Only replace this recipe's named children, never an override's parent.
    if path.parent != out or path.is_symlink():
        raise RuntimeError(f"Refusing to replace build path: {path}")
    if path.exists():
        shutil.rmtree(path)


@contextmanager
def output_lock(out: Path):
    import fcntl
    with (out / ".build.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Another lavapipe build is using {out}") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run(command: list[str], *, log: Path | None = None, **kwargs) -> None:
    display = shlex.join(command)
    print("+ " + (display if len(display) < 1600 else display[:1500] + " ..."), flush=True)
    if log is None:
        subprocess.run(command, check=True, text=True, **kwargs)
        return
    with log.open("w") as output:
        output.write("+ " + display + "\n")
        with subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, **kwargs) as process:
            assert process.stdout is not None
            for line in process.stdout:
                output.write(line)
                print(line, end="", flush=True)
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, command)


def capture(command: list[str], **kwargs) -> str:
    return subprocess.check_output(command, text=True, **kwargs).strip()


def host_tool(variable: str, default: str) -> str:
    selected = shutil.which(os.environ.get(variable, default))
    if not selected:
        raise RuntimeError(f"Build-host {default} is required (or set {variable})")
    return str(Path(selected).absolute())


def prepare_source(out: Path, name: str, patches: list[Path]) -> tuple[Path, str]:
    entry = PIN[name]
    root = entry["archive_root"]
    if Path(root).name != root or root in ("", ".", ".."):
        raise RuntimeError(f"Invalid pinned archive root: {root}")
    identity = fingerprint({"source": entry, "patches": {p.name: sha(p) for p in patches}})
    source = out / root
    stamp = out / (name + "-source.fingerprint")
    sentinel = source / ("meson.build" if name == "mesa" else "CMakeLists.txt")
    # Preserve every source mtime on a cache hit: both Ninja builds are expensive.
    if matched(stamp, identity, [sentinel]) and not source.is_symlink():
        return source, identity
    downloads = out / "downloads"
    downloads.mkdir(exist_ok=True)
    archive = downloads / (root + (".tar.xz" if name == "mesa" else ".tar.gz"))
    if not archive.is_file() or sha(archive) != entry["sha256"]:
        temporary = archive.with_name(archive.name + ".part")
        print(f"Downloading {entry['url']}", flush=True)
        try:
            with urllib.request.urlopen(entry["url"], timeout=120) as response:
                with temporary.open("wb") as destination:
                    shutil.copyfileobj(response, destination)
            if sha(temporary) != entry["sha256"]:
                raise RuntimeError(f"Source SHA256 mismatch: {archive.name}")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    # A failed extraction or patch must not destroy the previous working tree.
    with tempfile.TemporaryDirectory(prefix=name + "-extract-", dir=out) as directory:
        stage = Path(directory)
        with tarfile.open(archive) as contents:
            for member in contents.getmembers():
                if not Path(member.name).parts or Path(member.name).parts[0] != root:
                    raise RuntimeError(f"Unexpected archive member: {member.name}")
            contents.extractall(stage, filter="data")
        for patch in patches:
            run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)], cwd=stage / root)
        remove_directory(source, out)
        (stage / root).replace(source)
    write_if_changed(stamp, identity + "\n")
    return source, identity


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Cross-file flags are authoritative; user Python modules must not leak into
    # the isolated build-host environment.
    for name in ("PYTHONHOME", "PYTHONPATH", "CC", "CXX", "AR", "CFLAGS",
                 "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "PKG_CONFIG_PATH"):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def prepare_host_tools(out: Path, env: dict[str, str]) -> tuple[Path, dict]:
    requirements = HERE / "requirements-host.txt"
    venv = out / "venv"
    stamp = out / "host-tools.fingerprint"
    identity = fingerprint({"requirements": sha(requirements),
                            "python": sys.version, "executable": sys.executable})
    required = [venv / "bin" / name for name in ("python", "meson", "ninja")]
    if not matched(stamp, identity, required):
        remove_directory(venv, out)
        run([sys.executable, "-I", "-m", "venv", str(venv)], env=env)
        run([str(venv / "bin/python"), "-I", "-m", "pip", "install",
             "--disable-pip-version-check", "--requirement", str(requirements)],
            env=env, log=out / "host-tools.log")
        write_if_changed(stamp, identity + "\n")
    versions = json.loads(capture([str(venv / "bin/python"), "-I", "-c",
        "import importlib.metadata as m,json; print(json.dumps({"
        "n:m.version(n) for n in ['meson','ninja','Mako','MarkupSafe','PyYAML','packaging']}))"], env=env))
    return venv, {"python": sys.version.split()[0], "requirements_sha256": sha(requirements),
                  "packages": versions, "fingerprint": identity}


def strings(value: object, label: str) -> list[str]:
    if (not isinstance(value, list) or not value
            or not all(isinstance(item, str) and item and "\0" not in item
                       and "\n" not in item for item in value)):
        raise RuntimeError(f"LLVM metadata {label} must be a nonempty string list")
    return value


def static_archive(path: Path) -> None:
    if path.suffix != ".a" or not path.is_file():
        raise RuntimeError(f"Missing static archive: {path}")
    with path.open("rb") as archive:
        if archive.read(8) != b"!<arch>\n":
            raise RuntimeError(f"Expected a complete ordinary static archive: {path}")


def load_llvm(selected: str | None, no_run: bool) -> tuple[Path, dict]:
    if selected:
        metadata = Path(selected).absolute()
    else:
        recipe = REPO / PIN["llvm"]["recipe"]
        if not recipe.is_file():
            raise RuntimeError(f"LLVM recipe missing: {recipe}")
        command = ["bash", str(recipe)]
        if no_run:
            command.append("--no-run")
        # LLVM's own incremental recipe owns its build. The explicit metadata
        # option is for an already completed build and does not invoke LLVM.
        run(command, cwd=REPO)
        metadata = (Path(os.environ["LLVM_COSMO_OUT"]).absolute() / "LINK.json"
                    if os.environ.get("LLVM_COSMO_OUT") else REPO / PIN["llvm"]["metadata"])
    llvm = json.loads(metadata.read_text())
    llvm_recipe = (REPO / PIN["llvm"]["recipe"]).parent
    llvm_pin = json.loads((llvm_recipe / "IMPORT.json").read_text())
    current_patches = {p.name: sha(p) for p in sorted((llvm_recipe / "patches").glob("*.patch"))}
    expected_source = {"sources": llvm_pin["sources"], "patches": current_patches}
    if (llvm.get("sources") != expected_source["sources"]
            or llvm.get("patches") != current_patches
            or llvm.get("source_fingerprint") != fingerprint(expected_source)):
        raise RuntimeError("LLVM metadata does not match the current pinned sources and port patches; rebuild LLVM")
    for key, expected in (("format", 1), ("llvm_version", PIN["llvm"]["version"]),
                          ("target_triple", "x86_64-unknown-linux-musl"),
                          ("static", True),
                          ("rtti", False), ("exceptions", False)):
        if llvm.get(key) != expected:
            raise RuntimeError(f"LLVM metadata {key} must be {expected!r}")
    archives = strings(llvm.get("archives"), "archives")
    link_args = strings(llvm.get("link_args"), "link_args")
    for name in archives:
        if not Path(name).is_absolute() or name not in link_args:
            raise RuntimeError(f"LLVM archive missing from static link arguments: {name}")
        static_archive(Path(name))
    if {value for value in link_args if not value.startswith("-")} != set(archives):
        raise RuntimeError("LLVM link arguments do not match its archive list")
    verify_archives(archives, llvm.get("archive_sha256"),
                    required=llvm.get("runtime", {}).get("kind", "stock-sdk") != "stock-sdk")
    for argument in link_args:
        if re.search(r"\.(?:so(?:\.\d+)*|dylib|dll)(?:$|,)", argument, re.I):
            raise RuntimeError(f"Shared LLVM dependency is not allowed: {argument}")
    for name in strings(llvm.get("include_dirs"), "include_dirs"):
        if not Path(name).is_absolute() or not Path(name).is_dir():
            raise RuntimeError(f"Missing LLVM include directory: {name}")
    strings(llvm.get("cpp_args"), "cpp_args")
    if llvm.get("cpp_std") != "c++20":
        raise RuntimeError("Expected the pinned LLVM c++20 build metadata")
    for name in ("probe", "loader", "llvm_config"):
        if not Path(llvm.get(name, "")).is_file():
            raise RuntimeError(f"Missing LLVM metadata file: {name}")
    if sha(Path(llvm["probe"])) != llvm.get("probe_sha256"):
        raise RuntimeError("LLVM probe SHA256 does not match its completed build metadata")
    if not Path(llvm.get("licenses", "")).is_dir():
        raise RuntimeError("LLVM license tree is missing")
    return metadata, llvm


def sdk_tools(llvm: dict) -> tuple[Path, dict[str, str], str]:
    sdk = Path(llvm["sdk_root"]).absolute()
    if os.environ.get("COSMOCC") and Path(os.environ["COSMOCC"]).resolve() != sdk.resolve():
        raise RuntimeError("COSMOCC must match the SDK used by the completed LLVM build")
    names = {"c": "x86_64-unknown-cosmo-cc", "cpp": "x86_64-unknown-cosmo-c++",
             "ar": "x86_64-unknown-cosmo-ar", "strip": "x86_64-linux-cosmo-strip",
             "exe_wrapper": "ape-x86_64.elf"}
    tools = {key: str(sdk / "bin" / name) for key, name in names.items()}
    runtime = selected_runtime(sdk)
    if llvm.get("runtime", {"kind": "stock-sdk"}) != runtime:
        raise RuntimeError("LLVM metadata belongs to a different runtime; rebuild LLVM")
    version = "4.0.2" if runtime["kind"] == "stock-sdk" else "source-built"
    if llvm.get("cosmocc_version") != version:
        raise RuntimeError("LLVM metadata does not match the selected runtime profile")
    identity = {"runtime": runtime}
    for name in [*names.values(), "x86_64-linux-cosmo-gcc", "x86_64-linux-cosmo-g++"]:
        identity["bin/" + name] = sha(sdk / "bin" / name)
    for name in ("libexec/gcc/x86_64-linux-cosmo/14.1.0/cc1",
                 "libexec/gcc/x86_64-linux-cosmo/14.1.0/cc1plus",
                 "x86_64-linux-cosmo/lib/libcosmo.a", "x86_64-linux-cosmo/lib/libcxx.a"):
        identity[name] = sha(sdk / name)
    return sdk, tools, fingerprint(identity)


def wrap_sdk_tools(out: Path, tools: dict[str, str]) -> dict[str, str]:
    wrapped = tools.copy()
    # These SDK utilities are APE files. Python/Meson's direct exec does not
    # perform the shell fallback used by the compiler scripts on Linux.
    for name in ("ar", "strip"):
        wrapper = out / "bin" / ("cosmo-" + name)
        write_if_changed(wrapper, "#!/bin/sh\nexec "
                         + shlex.join([tools["exe_wrapper"], tools[name]]) + ' "$@"\n')
        wrapper.chmod(0o755)
        wrapped[name] = str(wrapper)
    return wrapped


def build_glslang(out: Path, source: Path, source_id: str, venv: Path,
                  llvm: dict, env: dict[str, str], jobs: int) -> tuple[Path, dict]:
    cmake = str(Path(llvm["host_tools"]["cmake"]).absolute())
    if not Path(cmake).is_file():
        raise RuntimeError(f"LLVM's build-host CMake is missing: {cmake}")
    ninja = str(venv / "bin/ninja")
    cc = host_tool("LLVM_HOST_CC", "cc")
    cxx = host_tool("LLVM_HOST_CXX", "c++")
    versions = {"cmake": capture([cmake, "--version"], env=env).splitlines()[0],
                "ninja": capture([ninja, "--version"], env=env),
                "cc": capture([cc, "--version"], env=env).splitlines()[0],
                "cpp": capture([cxx, "--version"], env=env).splitlines()[0]}
    build = out / "build-glslang"
    command = [cmake, "-S", str(source), "-B", str(build), "-G", "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={ninja}", f"-DCMAKE_C_COMPILER={cc}",
        f"-DCMAKE_CXX_COMPILER={cxx}", "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTING=OFF", "-DENABLE_OPT=OFF",
        "-DENABLE_HLSL=OFF", "-DENABLE_PCH=OFF", "-DGLSLANG_TESTS=OFF"]
    identity = fingerprint({"args": command, "source": source_id, "versions": versions,
                            "cc_sha256": sha(Path(cc)), "cpp_sha256": sha(Path(cxx))})
    stamp = out / "glslang-configuration.fingerprint"
    if not matched(stamp, identity, [build / "build.ninja"]):
        remove_directory(build, out)
        run(command, env=env, log=out / "glslang-configure.log")
        write_if_changed(stamp, identity + "\n")
    run([ninja, "-C", str(build), f"-j{jobs}", "glslang-standalone"],
        env=env, log=out / "glslang-build.log")
    binary = build / "StandAlone/glslang"
    if not binary.is_file():
        raise RuntimeError(f"Native glslang build did not produce {binary}")
    host_bin = out / "host-bin"
    host_bin.mkdir(exist_ok=True)
    alias = host_bin / "glslangValidator"
    if alias.is_symlink() and alias.resolve() != binary.resolve():
        alias.unlink()
    if not alias.is_symlink():
        if alias.exists():
            raise RuntimeError(f"Expected generated glslangValidator symlink: {alias}")
        alias.symlink_to(binary)
    return host_bin, {"path": str(binary), "sha256": sha(binary),
                      "configuration_fingerprint": identity, "versions": versions,
                      "cmake": cmake, "cc": cc, "cpp": cxx}


def meson_literal(value: object) -> str:
    if isinstance(value, str):
        if any(character in value for character in ("\n", "\r", "\0")):
            raise RuntimeError("Unsupported control character in Meson configuration")
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, list):
        return "[" + ", ".join(meson_literal(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    raise RuntimeError(f"Unsupported Meson configuration value: {value!r}")


def build_mesa(out: Path, source: Path, source_id: str, venv: Path,
               llvm: dict, tools: dict, sdk_id: str, host: dict,
               env: dict[str, str], jobs: int) -> Path:
    # Force this dependency: never use system LLVM discovery or a headers-only
    # stub. The real archive closure is also supplied to the final APE link.
    dependency = source / "subprojects/llvm/meson.build"
    include_args = ["-I" + name for name in llvm["include_dirs"]]
    include_args += ["-D__STDC_CONSTANT_MACROS", "-D__STDC_FORMAT_MACROS", "-D__STDC_LIMIT_MACROS"]
    write_if_changed(dependency,
        "# Generated from completed Cosmopolitan LLVM LINK.json.\n"
        "project('cosmopolitan-static-llvm', 'cpp', version: " + meson_literal(llvm["llvm_version"]) + ")\n"
        "has_rtti = false\n"
        "dep_llvm = declare_dependency(\n"
        "  version: " + meson_literal(llvm["llvm_version"]) + ",\n"
        "  compile_args: " + meson_literal(include_args) + ",\n"
        "  link_args: " + meson_literal(llvm["link_args"]) + ",\n)\n")
    c_args = ["-D_GNU_SOURCE", "-U__linux__", "-U__linux", "-Ulinux"]
    disabled_pkg_config = shutil.which("false")
    if not disabled_pkg_config:
        raise RuntimeError("The build-host false utility is required to disable target pkg-config")
    cross = out / "cosmo-x86_64.ini"
    write_if_changed(cross, "[binaries]\n" + "".join(
        key + " = " + meson_literal(value) + "\n" for key, value in tools.items())
        + "pkg-config = " + meson_literal(disabled_pkg_config) + "\n"
        "\n[host_machine]\nsystem = 'cosmopolitan'\ncpu_family = 'x86_64'\n"
        "cpu = 'x86_64'\nendian = 'little'\n"
        "\n[properties]\nneeds_exe_wrapper = true\n"
        "\n[built-in options]\nc_args = " + meson_literal(c_args) + "\n"
        "cpp_args = " + meson_literal(c_args + llvm["cpp_args"]) + "\n"
        "b_staticpic = false\nb_pie = false\n")
    build = out / "build-cosmo"
    command = [str(venv / "bin/meson"), "setup", str(build), str(source),
        "--cross-file", str(cross), "--wrap-mode=nofallback", "--force-fallback-for=llvm",
        "--buildtype=release", "-Ddefault_library=static", "-Dcpp_rtti=false",
        "-Dcpp_std=" + llvm["cpp_std"], "-Db_lto=false", "-Dplatforms=[]",
        "-Dgallium-drivers=[]", "-Dvulkan-drivers=swrast", "-Dvulkan-layers=[]",
        "-Dopengl=false", "-Dgles1=disabled", "-Dgles2=disabled", "-Degl=disabled",
        "-Dglx=disabled", "-Dgbm=disabled", "-Dglvnd=disabled", "-Dllvm=enabled",
        "-Dshared-llvm=disabled", "-Dllvm-orcjit=false", "-Dzlib=disabled",
        "-Dzstd=disabled", "-Dexpat=disabled", "-Dxmlconfig=disabled",
        "-Dshader-cache=disabled", "-Dlibunwind=disabled", "-Dlmsensors=disabled",
        "-Dgallium-rusticl=false", "-Dvideo-codecs=[]"]
    # Include generated dependency and SDK contents, not the changing probe log
    # or manifest timestamps, in the configuration identity.
    identity = fingerprint({"args": command, "source": source_id, "sdk": sdk_id,
        "cross": sha(cross), "dependency": sha(dependency), "host_glslang": host["sha256"],
        "host_tools": (out / "host-tools.fingerprint").read_text().strip()})
    stamp = out / "mesa-configuration.fingerprint"
    env["MESA_GIT_SHA1_OVERRIDE"] = source_id[:10]
    if not matched(stamp, identity, [build / "build.ninja"]):
        remove_directory(build, out)
        run(command, env=env, log=out / "mesa-configure.log")
        write_if_changed(stamp, identity + "\n")
    write_if_changed(out / "mesa-configure-args.json", json.dumps(command, indent=2) + "\n")
    run([str(venv / "bin/ninja"), "-C", str(build), f"-j{jobs}", MESA_TARGET],
        env=env, log=out / "mesa-build.log")
    archive = build / MESA_TARGET
    static_archive(archive)
    members = capture([tools["ar"], "t", str(archive)], env=env).splitlines()
    if not members or any(Path(name).suffix != ".o" for name in members):
        raise RuntimeError("Mesa's combined static archive must contain objects, not nested libraries")
    return archive


def compile_object(out: Path, name: str, source: Path, command: list[str],
                   inputs: list[Path], sdk_id: str, env: dict[str, str]) -> Path:
    output = out / (name + ".o")
    command = [*command, "-c", str(source), "-o", str(output)]
    identity = fingerprint({"command": command, "sdk": sdk_id,
                            "inputs": {str(path): sha(path) for path in [source, *inputs]}})
    stamp = out / (name + ".fingerprint")
    if not matched(stamp, identity, [output]):
        run(command, env=env)
        write_if_changed(stamp, identity + "\n")
    return output


def package(out: Path, source: Path, source_id: str, mesa: Path,
            llvm_path: Path, llvm: dict, sdk: Path, tools: dict, sdk_id: str,
            host: dict, python_tools: dict, env: dict[str, str], no_run: bool) -> None:
    common = [tools["c"], "-O2", "-g", "-fno-omit-frame-pointer", "-ffunction-sections",
              "-fdata-sections", "-I" + str(source / "include")]
    headers = [source / "include/vulkan/vulkan_core.h", source / "include/vulkan/vk_platform.h"]
    registration = compile_object(out, "register", HERE / "register.c", [*common,
        '-DCOSMO_LAVAPIPE_MESA_VERSION="' + PIN["mesa"]["version"] + '"'],
        [HERE / "register.h", HERE.parent / "wgpu_native/runtime/vulkan_loader.h", *headers],
        sdk_id, env)
    registration_archive = out / "libcosmo_lavapipe_registration.a"
    registration_id = sha(registration)
    registration_stamp = out / "registration-archive.fingerprint"
    if not matched(registration_stamp, registration_id, [registration_archive]):
        run([tools["ar"], "rcsD", str(registration_archive), str(registration)], env=env)
        write_if_changed(registration_stamp, registration_id + "\n")
    static_archive(registration_archive)
    mesa_args = ["-Wl,--whole-archive", str(mesa), "-Wl,--no-whole-archive", *llvm["link_args"]]
    link_args = [str(registration_archive), *mesa_args]
    verify_archives(llvm["archives"], llvm.get("archive_sha256"),
                    required=llvm.get("runtime", {}).get("kind", "stock-sdk") != "stock-sdk")
    link_archive_identity = static_archive_hashes([value for value in link_args if not value.startswith("-")])
    probe_object = compile_object(out, "probe", HERE / "probe.c", common, headers, sdk_id, env)
    probe = out / "lavapipe_probe.exe"
    debug_file = out / "lavapipe_probe.com.dbg"
    pe_setup = REPO / "third_party/wgpu_native/configure_pe.py"
    command = [tools["cpp"], str(probe_object), *mesa_args,
               "-Wl,--gc-sections", "-pthread", "-o", str(probe)]
    archive_hashes = {name: sha(Path(name)) for name in [str(mesa), *llvm["archives"]]}
    link_id = fingerprint({"command": command, "archives": archive_hashes,
                           "probe_object": sha(probe_object), "sdk": sdk_id, "pe_setup": sha(pe_setup)})
    link_stamp = out / "probe-link.fingerprint"
    if not matched(link_stamp, link_id, [probe, debug_file]):
        run(command, env=env, log=out / "probe-link.log")
        run([sys.executable, str(pe_setup), str(probe)], env=env)
        shutil.copy2(Path(str(probe) + ".dbg"), debug_file)
        write_if_changed(link_stamp, link_id + "\n")
    write_if_changed(out / "probe-link-args.json", json.dumps(command, indent=2) + "\n")
    loader = out / "ape-x86_64.elf"
    if not loader.is_file() or sha(loader) != sha(Path(tools["exe_wrapper"])):
        shutil.copy2(tools["exe_wrapper"], loader)
    loader.chmod(0o755)
    before = sha(probe)
    if not no_run:
        probe_env = env.copy()
        probe_env["LP_NUM_THREADS"] = "2"
        probe_env["MESA_SHADER_CACHE_DISABLE"] = "true"
        try:
            result = subprocess.run([str(loader), str(probe)], env=probe_env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            output += "\nStatic lavapipe probe timed out after 180 seconds.\n"
            (out / "probe.log").write_text(output)
            print(output, end="", flush=True)
            raise RuntimeError("Static lavapipe probe timed out") from error
        (out / "probe.log").write_text(result.stdout)
        print(result.stdout, end="", flush=True)
        result.check_returncode()
        if PROBE_PASS not in result.stdout.splitlines():
            raise RuntimeError("Static lavapipe probe did not confirm CPU enumeration")
        if sha(probe) != before:
            raise RuntimeError("The direct lavapipe probe modified its executable")
    licenses = out / "licenses"
    shutil.copytree(HERE / "licenses", licenses / ("mesa-" + PIN["mesa"]["version"]), dirs_exist_ok=True)
    shutil.copytree(llvm["licenses"], licenses / "llvm", dirs_exist_ok=True)
    shutil.copy2(HERE / "PIN.json", licenses / "LAVAPIPE-PIN.json")
    verify_archives([value for value in link_args if not value.startswith("-")], link_archive_identity)
    if sdk_tools(llvm)[2] != sdk_id:
        raise RuntimeError("The selected runtime changed during the Mesa build")
    manifest = {"format": 1, "mesa_version": PIN["mesa"]["version"],
        "runtime": selected_runtime(sdk),
        "llvm_version": llvm["llvm_version"],
        "provider": "embedded", "name": "Mesa lavapipe " + PIN["mesa"]["version"],
        "target": PIN["target"], "static": True, "external_vulkan_loader": False,
        "sources": {name: PIN[name] for name in ("mesa", "glslang")},
        "patches": {p.name: sha(p) for p in sorted((HERE / "patches").glob("*.patch"))},
        "source_fingerprint": source_id, "mesa_git_sha1_override": source_id[:10],
        "recipe_sha256": sha(HERE / "build.py"), "sdk_root": str(sdk), "sdk_fingerprint": sdk_id,
        "archive": str(mesa), "archive_sha256": archive_hashes[str(mesa)],
        "registration_archive": str(registration_archive),
        "registration_archive_sha256": sha(registration_archive),
        "include_dirs": [str(source / "include"), str(HERE)], "link_args": link_args,
        "link_archive_sha256": link_archive_identity,
        "host_glslang": host, "host_python_tools": python_tools,
        "llvm_metadata": str(llvm_path), "llvm_metadata_sha256": sha(llvm_path), "llvm": llvm,
        "probe": str(probe), "probe_sha256": before, "probe_debug": str(debug_file),
        "probe_run_on_build_host": not no_run, "loader": str(loader), "licenses": str(licenses)}
    write_if_changed(out / "LINK.json", json.dumps(manifest, indent=2) + "\n")
    print(f"Static lavapipe link manifest: {out / 'LINK.json'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="verify and patch sources without building LLVM or Mesa")
    parser.add_argument("--no-run", action="store_true", help="build probes without running them on this host")
    parser.add_argument("--llvm-metadata", default=os.environ.get("LAVAPIPE_LLVM_METADATA"),
                        help="use a completed LLVM LINK.json; also LAVAPIPE_LLVM_METADATA")
    args = parser.parse_args()
    out = Path(os.environ.get("LAVAPIPE_OUT", REPO / "o/lavapipe")).absolute()
    if out.is_symlink() or out.resolve() in (Path("/"), REPO, HERE, REPO / "o", HERE.parent):
        raise RuntimeError("LAVAPIPE_OUT must name a separate build directory")
    out.mkdir(parents=True, exist_ok=True)
    jobs = int(os.environ.get("LAVAPIPE_JOBS", "2"))
    if jobs < 1:
        raise RuntimeError("LAVAPIPE_JOBS must be positive")
    with output_lock(out):
        build(out, jobs, args)


def build(out: Path, jobs: int, args: argparse.Namespace) -> None:
    patches = sorted((HERE / "patches").glob("*.patch"))
    if not patches:
        raise RuntimeError("The checked-in Mesa Cosmopolitan patches are missing")
    source, source_id = prepare_source(out, "mesa", patches)
    glslang, glslang_id = prepare_source(out, "glslang", [])
    if args.prepare_only:
        print(f"Verified source trees: {source}, {glslang}")
        return
    llvm_path, llvm = load_llvm(args.llvm_metadata, args.no_run)
    sdk, tools, sdk_id = sdk_tools(llvm)
    tools = wrap_sdk_tools(out, tools)
    env = build_environment()
    venv, python_tools = prepare_host_tools(out, env)
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", os.defpath)
    host_bin, host = build_glslang(out, glslang, glslang_id, venv, llvm, env, jobs)
    env["PATH"] = str(host_bin) + os.pathsep + env["PATH"]
    mesa = build_mesa(out, source, source_id, venv, llvm, tools, sdk_id, host, env, jobs)
    package(out, source, source_id, mesa, llvm_path, llvm, sdk, tools, sdk_id,
            host, python_tools, env, args.no_run)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Cosmopolitan lavapipe build failed: {error}") from error
