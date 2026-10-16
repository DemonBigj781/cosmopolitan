"""Compile and execute independent probes using a real packaged toolchain APE."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
SAMPLES = Path(__file__).resolve().parent / "smoke"
PROBES = {
    "c": (["cc"], SAMPLES / "hello.c", "C SMOKE: 42\n"),
    "cpp": (["c++", "-std=c++17"], SAMPLES / "hello.cpp", "C++ SMOKE: 42\n"),
    "rust": (["rust", "build"], SAMPLES / "hello.rs", "RUST SMOKE: 42\n"),
    "machin": (["machin", "build"], ROOT / "toolchain/machin_demo.mfl",
               "Machin linked into Cosmopolitan APE\n"),
}
EXPECTED_COMPONENTS = {"c": ["C"], "cpp": ["C"], "rust": ["Rust", "C"], "machin": ["C"]}
OWNED_TEMP_PREFIXES = ("cosmo-sdk-", "rust-ape-build-", "machin-ape-")


def has_ape_magic(binary):
    # Signature check only; cross-platform execution remains a separate test.
    with binary.open("rb") as stream:
        return stream.read(8) in (b"MZqFpD='", b"jartsr='")


def probe(toolchain, language, output_dir, ape_loader=None):
    directory = output_dir / language
    directory.mkdir()  # Do not overwrite evidence from a previous run.
    temporary = directory / "temporary"
    temporary.mkdir()
    environment = dict(os.environ, TMPDIR=str(temporary))
    flags, source, expected = PROBES[language]
    binary = directory / "hello.com"
    prefix = [str(ape_loader)] if ape_loader else []
    command = [*prefix, str(toolchain), *flags, str(source), "-o", str(binary)]
    result = {"language": language, "command": command, "passed": False}
    start = time.monotonic()
    try:
        with (directory / "build.log").open("w") as log:
            build = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                   cwd=directory, timeout=600, env=environment)
        result["build_exit"] = build.returncode
        build_log = (directory / "build.log").read_text(errors="replace")
        components = [line.removeprefix("SDK materialized: ").split(" (", 1)[0]
                      for line in build_log.splitlines() if line.startswith("SDK materialized: ")]
        result["materialized_components"] = components
        if build.returncode:
            result["error"] = "compile/link failed; see build.log"
        elif not binary.is_file():
            result["error"] = "compiler reported success but produced no executable"
        elif not has_ape_magic(binary):
            result["error"] = "compiler output lacks the APE magic signature"
        elif components != EXPECTED_COMPONENTS[language]:
            result["error"] = "unexpected or repeated SDK component extraction"
        else:
            with (directory / "run.log").open("w") as log:
                run = subprocess.run([*prefix, str(binary)], stdout=log,
                                     stderr=subprocess.STDOUT, cwd=directory,
                                     timeout=60)
            result["run_exit"] = run.returncode
            output = (directory / "run.log").read_text()
            result["passed"] = run.returncode == 0 and output == expected
            if not result["passed"]:
                result["error"] = "exit status or exact output mismatch; see run.log"
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)
    leftovers = [path.name for path in temporary.iterdir()
                 if path.name.startswith(OWNED_TEMP_PREFIXES)]
    result["remaining_owned_temp_paths"] = leftovers
    if leftovers:
        result["passed"] = False
        result["error"] = result.get("error", "") + " owned temporary paths remain: " + repr(leftovers)
    result["seconds"] = round(time.monotonic() - start, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="New directory for binaries, logs, and summary.json")
    parser.add_argument("--language", choices=PROBES,
                        help="Run only one language; otherwise run all four serially")
    parser.add_argument("--ape-loader", type=Path,
                        help="Explicit native APE loader when direct execution is unavailable")
    args = parser.parse_args()
    toolchain = args.toolchain.resolve()
    if not toolchain.is_file() or not os.access(toolchain, os.X_OK):
        parser.error("--toolchain must name an existing executable")
    ape_loader = args.ape_loader.resolve() if args.ape_loader else None
    if ape_loader and (not ape_loader.is_file() or not os.access(ape_loader, os.X_OK)):
        parser.error("--ape-loader must name an existing executable")
    output_dir = args.output_dir.resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        parser.error(str(error))
    results = []
    for language in [args.language] if args.language else PROBES:
        result = probe(toolchain, language, output_dir, ape_loader)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status}: {language} ({result['seconds']}s)", flush=True)
        if "error" in result:
            print(f"  {result['error']}", flush=True)
        (output_dir / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"Evidence: {output_dir}")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
