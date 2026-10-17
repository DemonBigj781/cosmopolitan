"""Bundle unchanged CI-built programs and test them independently of compilers."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_metadata import sha256
from smoke_compilers import PROBES, has_ape_magic

LOADERS = ("ape-x86_64.elf", "ape-aarch64.elf", "ape-x86_64.macho", "ape-m1.c")
RUST_CHECKS = {
    "rust-std.com": {"expected_stdout": "RUST RELOCATED STD PASS: 42\n"},
    "rust-threads.com": {"expected_line": "threads ok"},
}


def windows_machine(path):
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError(f"Missing Windows header: {path.name}")
        stream.seek(struct.unpack_from("<I", header, 0x3c)[0])
        pe = stream.read(6)
    if len(pe) != 6 or pe[:4] != b"PE\0\0":
        raise ValueError(f"Invalid PE header: {path.name}")
    machine = {0x8664: "x86_64", 0xaa64: "aarch64"}.get(struct.unpack_from("<H", pe, 4)[0])
    if not machine:
        raise ValueError(f"Unsupported Windows machine: {path.name}")
    return machine


def create_bundle(smoke, rust, loaders, output, source_commit):
    rust_build = json.loads((rust / "BUILD.json").read_text())
    loader_build = json.loads((loaders / "BUILD.json").read_text())
    if any(build["source_commit"] != source_commit for build in (rust_build, loader_build)):
        raise ValueError("Build revision mismatch")
    entries = []
    for language, (_, _, expected) in PROBES.items():
        source = smoke / f"compiler-smoke-{language}"
        results = json.loads((source / "summary.json").read_text())
        if (len(results) != 1 or results[0].get("passed") is not True or
                results[0].get("language") != language or
                results[0].get("build_exit") != 0 or results[0].get("run_exit") != 0):
            raise ValueError(f"Failed or missing compiler probe: {language}")
        entries.append((source / language / "hello.com", f"{language}-smoke.com",
                        {"expected_stdout": expected}))
    for name, check in RUST_CHECKS.items():
        records = [p for p in rust_build["programs"] if p["file"] == name]
        if len(records) != 1 or records[0]["sha256"] != sha256(rust / name):
            raise ValueError(f"Rust program hash mismatch: {name}")
        entries.append((rust / name, name, check))
    resources = [(loaders / name, name) for name in LOADERS]
    resources += [(loaders / "LICENSE.machin", "LICENSE.machin"),
                  (rust / "LICENSE.rust-ape", "LICENSE.rust-ape")]
    manifest = {"schema_version": 1, "source_commit": source_commit,
                "scope": "Generated-program execution; not compiler-host portability",
                "programs": [], "resources": {}}
    for source, name, check in entries:
        if not has_ape_magic(source):
            raise ValueError(f"Not an APE program: {name}")
        manifest["programs"].append({"file": name, "sha256": sha256(source),
                                     "windows_machine": windows_machine(source), **check})
    for source, name in resources:
        if not source.stat().st_size:
            raise ValueError(f"Empty runtime resource: {name}")
        manifest["resources"][name] = sha256(source)
    output.mkdir(parents=True, exist_ok=False)
    for source, name in [(src, name) for src, name, _ in entries] + resources:
        shutil.copyfile(source, output / name)
    (output / "BUILD.json").write_text(json.dumps(manifest, indent=2) + "\n")
    validate_bundle(output, source_commit)
    print(f"Bundled {len(entries)} unchanged programs for runtime testing")


def validate_bundle(bundle, source_commit):
    manifest = json.loads((bundle / "BUILD.json").read_text())
    if manifest.get("schema_version") != 1 or manifest["source_commit"] != source_commit:
        raise ValueError("Unexpected bundle schema or source revision")
    files = [(p["file"], p["sha256"]) for p in manifest["programs"]]
    files += list(manifest["resources"].items())
    if len(files) != len(set(name for name, _ in files)) or not manifest["programs"]:
        raise ValueError("Empty or duplicate bundle inventory")
    for name, digest in files:
        if not name or name in (".", "..") or Path(name).name != name or any(c in name for c in "\\:"):
            raise ValueError(f"Unsafe bundle path: {name}")
        if sha256(bundle / name) != digest:
            raise ValueError(f"Bundle hash mismatch: {name}")
    for program in manifest["programs"]:
        if program["windows_machine"] != windows_machine(bundle / program["file"]):
            raise ValueError("Windows architecture differs from manifest")
    return manifest


def host_platform():
    system = platform.system()
    if system == "Windows":
        # Query the native OS, even when Python itself runs under x64 emulation.
        info = ctypes.create_string_buffer(64)
        ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(info))
        architecture = {9: "x86_64", 12: "aarch64"}.get(struct.unpack_from("<H", info.raw)[0])
    else:
        architecture = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "aarch64",
                        "aarch64": "aarch64"}.get(platform.machine())
    if not architecture:
        raise ValueError("Unsupported host architecture")
    return system, architecture


def prepare_loader(bundle, output, system, architecture):
    if system == "Windows":
        return None
    if system == "Darwin" and architecture == "aarch64":
        target = output / "ape-arm64"
        with (output / "loader-build.log").open("w") as log:
            subprocess.run(["cc", "-O", "-arch", "arm64", "-o", str(target), str(bundle / "ape-m1.c")],
                           check=True, timeout=180, stdout=log, stderr=subprocess.STDOUT)
    else:
        name = f"ape-{architecture}.elf" if system == "Linux" else "ape-x86_64.macho"
        target = output / name
        shutil.copyfile(bundle / name, target)
    target.chmod(0o755)
    return target


def run_suite(bundle, output, source_commit, expected_os, expected_arch):
    bundle, output = bundle.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"source_commit": source_commit, "scope": "Generated programs only",
               "passed": False, "programs": []}
    try:
        system, architecture = host_platform()
        summary["host"] = {"os": system, "architecture": architecture,
                           "python_architecture": platform.machine(), "release": platform.release()}
        if system not in ("Linux", "Darwin", "Windows") or (system, architecture) != (expected_os, expected_arch):
            raise ValueError(f"Unexpected host: {system}/{architecture}")
        manifest = validate_bundle(bundle, source_commit)
        manifest_hash = sha256(bundle / "BUILD.json")
        loader = prepare_loader(bundle, output, system, architecture)
        for program in manifest["programs"]:
            name = program["file"]
            directory = output / Path(name).stem
            directory.mkdir()
            temporary = directory / "temporary"
            temporary.mkdir()
            executable = directory / (Path(name).stem + ".exe" if system == "Windows" else name)
            shutil.copyfile(bundle / name, executable)
            executable.chmod(0o755)
            execution = "native"
            if system == "Windows" and architecture != program["windows_machine"]:
                if architecture != "aarch64" or program["windows_machine"] != "x86_64":
                    raise ValueError("Unsupported Windows execution architecture")
                execution = "x64-emulated"
            record = {"file": name, "execution": execution, "passed": False,
                      "sha256_before": sha256(executable)}
            summary["programs"].append(record)
            command = ([str(loader)] if loader else []) + [str(executable)]
            record["command"] = command
            environment = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
            start = time.monotonic()
            try:
                if record["sha256_before"] != program["sha256"]:
                    raise ValueError("Executable copy differs from build artifact")
                with (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
                    process = subprocess.run(command, cwd=directory, env=environment,
                                             stdout=stdout, stderr=stderr, timeout=180)
                record["exit_code"] = process.returncode
                text = (directory / "stdout.log").read_text(errors="replace")
                matched = (text == program["expected_stdout"] if "expected_stdout" in program
                           else program["expected_line"] in text.splitlines())
                record["output_matched"] = matched
                record["passed"] = process.returncode == 0 and matched
                if not record["passed"]:
                    record["error"] = "Nonzero exit or unexpected output; see logs"
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                record["error"] = str(error)
            record["seconds"] = round(time.monotonic() - start, 3)
            record["sha256_after"] = sha256(executable)
            if record["sha256_after"] != program["sha256"]:
                record.update(passed=False, error="Executable bytes changed during execution")
            print(f"{'PASS' if record['passed'] else 'FAIL'}: {name} ({execution})", flush=True)
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        validate_bundle(bundle, source_commit)
        if sha256(bundle / "BUILD.json") != manifest_hash:
            raise ValueError("Bundle manifest changed during testing")
        summary["passed"] = all(p["passed"] for p in summary["programs"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        summary["error"] = str(error)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle")
    for name in ("smoke", "rust", "loaders"):
        bundle.add_argument("--" + name, type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--bundle", type=Path, required=True)
    run.add_argument("--expected-os", choices=("Linux", "Darwin", "Windows"), required=True)
    run.add_argument("--expected-arch", choices=("x86_64", "aarch64"), required=True)
    for command in (bundle, run):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    if args.command == "bundle":
        create_bundle(args.smoke, args.rust, args.loaders, args.output, args.source_commit)
        return 0
    summary = run_suite(args.bundle, args.output, args.source_commit, args.expected_os, args.expected_arch)
    if "error" in summary:
        print(summary["error"], file=sys.stderr)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
