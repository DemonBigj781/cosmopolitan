#!/usr/bin/env python3
"""Require real Linux chroot execution of the embedded software Vulkan tests.

The host Python runner retains logs outside each temporary root. The executed
program sees only the copied APE, its explicit loader, and writable /tmp.
This is a deployment-dependency check, not a sandbox for untrusted programs.
"""
# SPDX-License-Identifier: MIT

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


ARTIFACTS = ("webgpu_compute", "ape-x86_64.elf")
ROOT_CONTENTS = set(ARTIFACTS) | {"tmp"}
LOG_LIMIT = 16 * 1024 * 1024
COMPUTE_PASS = "headless Vulkan compute and readback: PASS"
RASTER_PASS = "headless Vulkan triangle raster and readback: PASS"


class IsolationFailure(Exception):
    exit_code = 1


class IsolationUnavailable(IsolationFailure):
    exit_code = 3


def isolated_environment():
    # No inherited loader paths, driver paths, credentials, shell, or home.
    # Cache files, if any, must stay within the only writable work directory.
    return {
        "PATH": "/no-tools",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/tmp/cache",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "TERM": "dumb",
        "COSMO_WGPU_TRACE": "1",
        "MESA_SHADER_CACHE_DISABLE": "true",
    }


def worker(arguments):
    """Enter the root in a child, then replace Python with the static loader."""
    if len(arguments) < 2 or arguments[0] not in (
            "probe", "collatz", "matmul", "raster"):
        print("Invalid internal isolation worker invocation", file=sys.stderr)
        return 125
    mode, root = arguments[:2]
    if len(arguments) != 2 or not os.path.isabs(root):
        print("Invalid internal isolation root", file=sys.stderr)
        return 125
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (LOG_LIMIT, LOG_LIMIT))
        # This actual syscall is required for both the probe and every run.
        # Python and its host libraries are replaced by execve afterwards.
        os.chroot(root)
        os.chdir("/")
    except OSError as error:
        print(f"Isolation unavailable: chroot failed: {error}", file=sys.stderr)
        return 125
    if set(os.listdir("/")) != ROOT_CONTENTS:
        print("Isolation root has unexpected contents", file=sys.stderr)
        return 125
    if mode == "probe":
        root_stat = os.stat("/")
        print(f"CHROOT_CONFIRMED {root_stat.st_dev}:{root_stat.st_ino}")
        return 0
    command = ["/ape-x86_64.elf", "/webgpu_compute", "--software"]
    if mode == "matmul":
        command.append("--matmul")
    elif mode == "raster":
        command.append("--raster")
    try:
        os.execve(command[0], command, isolated_environment())
    except OSError as error:
        print(f"Isolated executable could not start: {error}", file=sys.stderr)
        return 126


def sha256(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as data:
        if not stat.S_ISREG(os.fstat(data.fileno()).st_mode):
            raise IsolationFailure(f"Expected a regular artifact: {path}")
        for block in iter(lambda: data.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hashes(directory):
    result = {}
    for name in ARTIFACTS:
        path = directory / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise IsolationFailure(f"Artifact must be a regular, non-symlink file: {path}")
        result[name] = sha256(path)
    return result


def verify_manifest(directory, baseline):
    manifest = directory / "SHA256SUMS"
    if manifest.is_symlink():
        raise IsolationFailure("SHA256SUMS must not be a symlink")
    if not manifest.exists():
        return False
    if not stat.S_ISREG(manifest.lstat().st_mode):
        raise IsolationFailure("SHA256SUMS must be a regular, non-symlink file")
    expected = {}
    for line in manifest.read_text().splitlines():
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if not match:
            raise IsolationFailure("Malformed SHA256SUMS entry")
        digest, name = match.groups()
        if name in ARTIFACTS:
            if name in expected:
                raise IsolationFailure(f"Duplicate SHA256SUMS entry for {name}")
            expected[name] = digest.lower()
    if expected != baseline:
        raise IsolationFailure("Artifact hashes do not match SHA256SUMS")
    return True


def copy_artifact(source, destination):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as data:
        if not stat.S_ISREG(os.fstat(data.fileno()).st_mode):
            raise IsolationFailure(f"Expected a regular artifact: {source}")
        with destination.open("xb") as output:
            shutil.copyfileobj(data, output, length=1024 * 1024)
    destination.chmod(0o755)


def kill_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise IsolationFailure("An isolated process did not exit after SIGKILL")


def capture(mode, root, log_path, timeout):
    command = [sys.executable, "-I", "-S", str(Path(__file__).resolve()),
               "--_isolation-worker", mode, str(root)]
    started = time.monotonic()
    timed_out = False
    with log_path.open("xb") as log:
        os.fchmod(log.fileno(), 0o644)
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, env=isolated_environment(),
            close_fds=True, start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_group(process)
            returncode = process.returncode
        except BaseException:
            kill_process_group(process)
            raise
    return {
        "exit_code": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path),
    }


def validate_output(mode, text):
    lines = text.splitlines()
    errors = []
    providers = [line for line in lines if line.startswith("Vulkan provider: ")]
    if len(providers) != 1 or not re.fullmatch(
            r"Vulkan provider: embedded \(.+\)", providers[0]):
        errors.append("missing or inconsistent embedded-provider identity")
    opens = [line for line in lines if line.startswith("native Vulkan loader opens:")]
    if opens != ["native Vulkan loader opens: 0"]:
        errors.append("native Vulkan loader open count is not exactly zero")
    if not any(re.fullmatch(
            r"WebGPU: Vulkan provider=embedded; implementation=.+; native loader bypassed",
            line) for line in lines):
        errors.append("missing embedded entry trace")
    if any("WebGPU: Vulkan provider=native;" in line for line in lines):
        errors.append("native Vulkan provider was attempted")
    if not any(line.startswith("adapter: ") and line.endswith("; type=CPU/software")
               for line in lines):
        errors.append("adapter is not identified as CPU/software")
    pass_marker = RASTER_PASS if mode == "raster" else COMPUTE_PASS
    for marker in ("C -> Rust version check: PASS", pass_marker):
        if marker not in lines:
            errors.append(f"missing result marker: {marker}")
    if mode == "collatz":
        for marker in ("input: [1, 2, 3, 4]", "readback: [0, 1, 7, 2]"):
            if marker not in lines:
                errors.append(f"missing numerical output: {marker}")
    elif mode == "matmul":
        matrix = [re.fullmatch(
            r"f32 matrix multiplication: 13x17 \* 17x11; "
            r"143 results verified; max error ([0-9.eE+\-]+); "
            r"shared memory, barriers, and partial tiles: PASS", line)
            for line in lines]
        matrix = [match for match in matrix if match]
        if len(matrix) != 1:
            errors.append("missing matrix numerical/shared-memory verification")
        else:
            try:
                error = float(matrix[0].group(1))
                if not math.isfinite(error) or error < 0:
                    raise ValueError
            except ValueError:
                errors.append("matrix reported an invalid maximum numerical error")
    else:
        raster = [re.fullmatch(
            r"rasterized triangle: 64x64 RGBA8; ([0-9]+) covered pixels; "
            r"center rgba=\[([0-9]+), ([0-9]+), ([0-9]+), ([0-9]+)\]; "
            r"clear corners: PASS", line)
            for line in lines]
        raster = [match for match in raster if match]
        if len(raster) != 1:
            errors.append("missing triangle raster/readback verification")
        else:
            covered, red, green, blue, alpha = map(int, raster[0].groups())
            if not 800 <= covered <= 1400:
                errors.append("triangle reported implausible pixel coverage")
            if not (228 <= red <= 232 and 49 <= green <= 53 and
                    24 <= blue <= 28 and alpha == 255):
                errors.append("triangle center pixel did not match the shader color")
    return errors


def verify(directory, timeout):
    log_directory = Path(tempfile.mkdtemp(prefix="isolated-linux-", dir=directory))
    # A sudo-run verifier must leave its sanitized logs readable by the normal
    # CI artifact uploader. The temporary chroot directories remain private.
    log_directory.chmod(0o755)
    report_path = log_directory / "report.json"
    report = {
        "format": 1,
        "status": "failed",
        "artifact_directory": str(directory),
        "platform": platform.platform(),
        "timeout_seconds_per_test": timeout,
        "environment": isolated_environment(),
        "runs": [],
    }
    baseline = None
    exit_code = 1
    try:
        if platform.system() != "Linux":
            raise IsolationUnavailable("This verifier requires Linux and actual chroot capability")
        baseline = hashes(directory)
        report["sha256_before"] = baseline
        report["sha256_manifest_verified"] = verify_manifest(directory, baseline)
        for mode in ("collatz", "matmul", "raster"):
            with tempfile.TemporaryDirectory(prefix=f"root-{mode}-", dir=log_directory) as temp:
                root = Path(temp)
                for name in ARTIFACTS:
                    copy_artifact(directory / name, root / name)
                (root / "tmp").mkdir(mode=0o700)
                (root / "tmp").chmod(0o1777)
                copied_before = hashes(root)
                if copied_before != baseline:
                    raise IsolationFailure("Copied artifacts differ from their initial hashes")
                if mode == "collatz":
                    capability = capture("probe", root, log_directory / "chroot.log", 10)
                    report["chroot_capability"] = capability
                    probe = Path(capability["log"]).read_text(errors="replace").strip()
                    root_stat = root.stat()
                    expected_probe = f"CHROOT_CONFIRMED {root_stat.st_dev}:{root_stat.st_ino}"
                    if capability["exit_code"] != 0 or capability["timed_out"] or probe != expected_probe:
                        raise IsolationUnavailable(
                            f"Actual chroot isolation was not established: {probe or 'no confirmation'}"
                        )
                run = capture(mode, root, log_directory / f"{mode}.log", timeout)
                run["name"] = mode
                workload_argument = {
                    "collatz": [],
                    "matmul": ["--matmul"],
                    "raster": ["--raster"],
                }[mode]
                run["arguments"] = ["--software", *workload_argument]
                run["root_initial_contents"] = sorted(ROOT_CONTENTS)
                run["sha256_before"] = copied_before
                run["errors"] = []
                report["runs"].append(run)
                if run["timed_out"]:
                    run["errors"].append(f"execution exceeded {timeout} seconds")
                if run["exit_code"] != 0:
                    run["errors"].append(f"executable exited with status {run['exit_code']}")
                run["sha256_after"] = hashes(root)
                if run["sha256_after"] != baseline:
                    run["errors"].append("an isolated executable changed during execution")
                if set(os.listdir(root)) != ROOT_CONTENTS:
                    run["errors"].append("execution created files outside /tmp")
                text = Path(run["log"]).read_text(errors="replace")
                run["errors"].extend(validate_output(mode, text))
                run["status"] = "failed" if run["errors"] else "passed"
                print(f"{mode}: exit={run['exit_code']}; log={run['log']}", flush=True)
        if any(run["errors"] for run in report["runs"]):
            raise IsolationFailure("One or more isolated software WebGPU checks failed")
        report["status"] = "passed"
        exit_code = 0
    except (IsolationFailure, OSError, ValueError) as error:
        report["error"] = str(error)
        exit_code = getattr(error, "exit_code", 1)
    finally:
        if baseline is not None:
            try:
                report["sha256_after"] = hashes(directory)
                if report["sha256_after"] != baseline:
                    raise IsolationFailure("Source artifacts changed during isolated verification")
            except (IsolationFailure, OSError) as error:
                report["status"] = "failed"
                report["error"] = str(error)
                exit_code = 1
        with report_path.open("x") as destination:
            os.fchmod(destination.fileno(), 0o644)
            json.dump(report, destination, indent=2)
            destination.write("\n")
    if exit_code:
        print(f"Isolated Linux software verification: FAILED: {report.get('error', 'unknown error')}",
              file=sys.stderr)
        for run in report["runs"]:
            for run_error in run["errors"]:
                print(f"  {run['name']}: {run_error}", file=sys.stderr)
    else:
        print("Isolated Linux software verification: PASS "
              "(actual chroot, compute and raster checks, unchanged hashes)")
    print(f"Isolation report: {report_path}", flush=True)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True,
                        help="directory containing webgpu_compute and ape-x86_64.elf")
    parser.add_argument("--timeout", type=int, default=180,
                        help="per-workload timeout in seconds, between 1 and 600 (default: 180)")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    try:
        directory = args.artifact_dir.expanduser().resolve(strict=True)
        if not directory.is_dir():
            parser.error("--artifact-dir must name a directory")
        return verify(directory, args.timeout)
    except OSError as error:
        print(f"Isolation verifier could not initialize: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_isolation-worker":
        sys.exit(worker(sys.argv[2:]))
    sys.exit(main())
