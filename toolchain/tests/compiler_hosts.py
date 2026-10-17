"""Probe the actual packaged compiler independently on each supported CI host."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from program_matrix import host_platform, prepare_loader, validate_bundle
from file_metadata import sha256
from smoke_compilers import PROBES, ROOT, probe


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
            record = {"name": name, "passed": False}
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
            print(f"{'PASS' if record['passed'] else 'FAIL'}: launcher {name}", flush=True)
            (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        for language in PROBES:
            record = probe(executable, language, output, loader)
            result["compilers"].append(record)
            print(f"{'PASS' if record['passed'] else 'FAIL'}: compiler {language}", flush=True)
            (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        result["toolchain_unchanged"] = sha256(executable) == sha256(toolchain) == original_hash
        result["passed"] = (result["toolchain_unchanged"] and all(r["passed"] for r in result["launcher"])
                            and all(r["passed"] for r in result["compilers"]))
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
