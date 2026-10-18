"""Probe the actual packaged compiler independently on each supported CI host."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from program_matrix import host_platform, prepare_loader, validate_bundle, windows_machine
from file_metadata import sha256
from smoke_compilers import PROBES, ROOT, SAMPLES, OWNED_TEMP_PREFIXES, probe


def check_cargo_project(toolchain, output, loader):
    project = output / "cargo-check"
    shutil.copytree(SAMPLES / "cargo-host", project,
                    ignore=shutil.ignore_patterns("target", "__pycache__"))
    temporary = project / "temporary"
    temporary.mkdir()
    target = project / "target"
    environment = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
                       CARGO_HOME=str(project / "cargo-home"))
    for variable in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        environment.pop(variable, None)
    command = ([str(loader)] if loader else []) + [str(toolchain), "rust", "cargo", "--offline", "check",
                "--manifest-path", str(project / "Cargo.toml"), "--target-dir", str(target)]
    result = {"passed": False, "command": command}
    try:
        with (project / "check.log").open("w") as log:
            run = subprocess.run(command, cwd=project, env=environment, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=600)
        result["exit_code"] = run.returncode
        metadata = list(target.glob("debug/deps/libcargo_host_probe-*.rmeta"))
        result["metadata"] = [{"file": str(p.relative_to(project)), "sha256": sha256(p)}
                              for p in metadata if p.stat().st_size]
        text = (project / "check.log").read_text(errors="replace")
        result["materialized_components"] = [line.split(" (", 1)[0].removeprefix("SDK materialized: ")
            for line in text.splitlines() if line.startswith("SDK materialized: ")]
        result["remaining_owned_temp_paths"] = [p.name for p in temporary.iterdir()
                                               if p.name.startswith(OWNED_TEMP_PREFIXES)]
        result["passed"] = (run.returncode == 0 and bool(result["metadata"]) and
            result["materialized_components"] == ["Rust"] and not result["remaining_owned_temp_paths"])
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def check_host(toolchain, build_manifest, resources, output, commit, expected_os, expected_arch):
    toolchain, output = toolchain.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {"source_commit": commit, "passed": False, "launcher": [], "compilers": []}
    try:
        system, architecture = host_platform()
        result["host"] = {"os": system, "architecture": architecture}
        if (system, architecture) != (expected_os, expected_arch):
            raise ValueError("Unexpected compiler-test host")
        build = json.loads(build_manifest.read_text())
        if build["source_commit"] != commit:
            raise ValueError("Unexpected toolchain source revision")
        original_hash = sha256(toolchain)
        if original_hash != build["artifact"]["sha256"]:
            raise ValueError("Toolchain hash differs from build")
        rust_host = ("x86_64-pc-windows-msvc" if system == "Windows" else
                     architecture + ("-apple-darwin" if system == "Darwin" else "-unknown-linux-gnu"))
        if rust_host not in build["inputs"]["rust_compiler"]["hosts"]:
            raise ValueError("Rust payload missing for compiler-test host: " + rust_host)
        result["rust_compiler_host"] = rust_host
        result["rust_compiler_supported"] = True
        result["toolchain_execution"] = ("x64-emulated" if system == "Windows" and
            architecture == "aarch64" and windows_machine(toolchain) == "x86_64" else "native")
        validate_bundle(resources, commit)
        loader = prepare_loader(resources.resolve(), output, system, architecture)
        executable = toolchain
        if system == "Windows":
            executable = output / "toolchain.exe"
            shutil.copyfile(toolchain, executable)
        executable.chmod(0o755)
        prefix = ([str(loader)] if loader else []) + [str(executable)]
        launch_dir = output / "launcher"
        launch_dir.mkdir()
        temporary = launch_dir / "temporary"
        temporary.mkdir()
        environment = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        checks = [
            ("version", ["version"], "ape-toolchain"),
            ("selftest", ["selftest"], "PASS"),
            ("rust-status", ["rust", "status"], "Resource access: ZIPOS"),
            ("machin-compiler", ["machin-compiler", "--full", str(ROOT / "toolchain/machin_demo.mfl")], "int main("),
            ("cc-version", ["cc", "--version"], ""),
            ("rustc-version", ["rust", "rustc", "--version"], ""),
            ("cargo-version", ["rust", "cargo", "--version"], ""),
        ]
        for name, arguments, marker in checks:
            record = {"name": name, "passed": False, "supported": True}
            try:
                with (launch_dir / (name + ".stdout.log")).open("w") as stdout, \
                     (launch_dir / (name + ".stderr.log")).open("w") as stderr:
                    run = subprocess.run(prefix + arguments, stdout=stdout, stderr=stderr,
                                         env=environment, cwd=launch_dir, timeout=180)
                text = (launch_dir / (name + ".stdout.log")).read_text(errors="replace")
                record.update(exit_code=run.returncode, passed=run.returncode == 0 and bool(text) and marker in text)
            except (OSError, subprocess.SubprocessError) as error:
                record["error"] = str(error)
            result["launcher"].append(record)
            status = "PASS" if record["passed"] else "FAIL"
            print(f"{status}: launcher {name}", flush=True)
            (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        for language in PROBES:
            record = probe(executable, language, output, loader)
            record["supported"] = True
            result["compilers"].append(record)
            status = "PASS" if record["passed"] else "FAIL"
            print(f"{status}: compiler {language}", flush=True)
            (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        result["cargo"] = check_cargo_project(executable, output, loader)
        print(f"{'PASS' if result['cargo']['passed'] else 'FAIL'}: Cargo offline native-std check", flush=True)
        result["toolchain_unchanged"] = sha256(executable) == sha256(toolchain) == original_hash
        result["passed"] = (result["toolchain_unchanged"] and
            all(r["passed"] for r in result["launcher"] + result["compilers"]) and result["cargo"]["passed"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("toolchain", "build-manifest", "resources", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("source-commit", "expected-os", "expected-arch"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = check_host(args.toolchain, args.build_manifest, args.resources, args.output,
                        args.source_commit, args.expected_os, args.expected_arch)
    if "error" in result:
        print(result["error"], file=sys.stderr)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
