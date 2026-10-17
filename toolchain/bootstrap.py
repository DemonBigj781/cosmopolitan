"""Pinned CI bootstrap. Existing destinations are never trusted or overwritten."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

from file_metadata import STAMP, sha256, tree_sha256

ROOT = Path(__file__).resolve().parents[1]
RUST_NOTICES = (
    "share/doc/cargo/LICENSE-APACHE",
    "share/doc/cargo/LICENSE-MIT",
    "share/doc/cargo/LICENSE-THIRD-PARTY",
    "share/doc/rust/COPYRIGHT-library.html",
    "share/doc/rust/COPYRIGHT.html",
)


def capture(command, **kwargs):
    return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def load_pins(root=ROOT):
    pins = json.loads((root / "toolchain/bootstrap.lock.json").read_text())
    upstream = root / "third_party/rust_ape/upstream"
    config = (upstream / "xtask/src/config.rs").read_text()
    sdk = {}
    # Python re, anchored Rust const declarations; duplicates must fail closed.
    for key in ("version", "sha256"):
        matches = re.findall(r'^pub const COSMOCC_' + key.upper() + r': &str =\s*"([^"\n]+)";', config, re.M)
        if len(matches) != 1:
            raise ValueError(f"Expected one COSMOCC_{key.upper()} declaration")
        sdk[key] = matches[0]
    pins["cosmocc"] = sdk
    pins["rust_nightly"] = tomllib.loads((upstream / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    for version in (pins["go_version"], pins["rust_linker_probe"], sdk["version"]):
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError(f"Expected exact version: {version}")
    if not re.fullmatch(r"[0-9a-f]{40}", pins["machin"]["revision"]):
        raise ValueError("Machin must use an exact commit")
    if not re.fullmatch(r"[0-9a-f]{64}", sdk["sha256"]):
        raise ValueError("Cosmocc must have a SHA256 pin")
    if not re.fullmatch(r"nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}", pins["rust_nightly"]):
        raise ValueError("Rust nightly must use an exact date")
    return pins


def require_new(path):
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Refusing existing bootstrap destination: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def stamp(root, **metadata):
    value = {"schema_version": 1, **metadata, "tree_sha256": tree_sha256(root)}
    (root / STAMP).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def bootstrap_sdk(destination, pins, root=ROOT):
    destination = destination.absolute()
    require_new(destination)
    # The existing downloader also updates a sibling `current` link. Isolate it.
    with tempfile.TemporaryDirectory(prefix="cosmocc-bootstrap-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "sdk"
        subprocess.run(["sh", str(root / "build/download-cosmocc.sh"), str(staged),
                        pins["cosmocc"]["version"], pins["cosmocc"]["sha256"]], cwd=root, check=True)
        for name in ("bin/cosmocc", "bin/zipcopy"):
            if not (staged / name).is_file():
                raise FileNotFoundError(staged / name)
        stamp(staged, cosmocc=pins["cosmocc"], verification="archive-sha256-checked")
        require_new(destination)
        staged.rename(destination)


def bootstrap_machin(source, binary, pins):
    source, binary = source.absolute(), binary.absolute()
    require_new(source)
    require_new(binary)
    environment = dict(os.environ, GOTOOLCHAIN="local")
    version = capture(["go", "version"], env=environment)
    if version.split()[2:3] != ["go" + pins["go_version"]]:
        raise ValueError(f"Go version does not match pin: {version}")
    with tempfile.TemporaryDirectory(prefix="machin-bootstrap-", dir=source.parent) as temporary:
        staged = Path(temporary) / "source"
        staged.mkdir()
        for command in (["git", "init", "--quiet"],
                        ["git", "remote", "add", "origin", pins["machin"]["repository"]],
                        ["git", "fetch", "--depth=1", "origin", pins["machin"]["revision"]],
                        ["git", "switch", "--detach", "FETCH_HEAD"]):
            subprocess.run(command, cwd=staged, check=True)
        if capture(["git", "rev-parse", "HEAD"], cwd=staged) != pins["machin"]["revision"]:
            raise ValueError("Machin checkout does not match pin")
        # Local calls are single-worker even when the caller omits build controls.
        environment.update(GOMAXPROCS="1", GOFLAGS="-p=1")
        with tempfile.TemporaryDirectory(prefix="machin-output-", dir=binary.parent) as output:
            executable = Path(output) / "machin-compiler"
            subprocess.run(["go", "build", "-p=1", "-trimpath", "-o", str(executable), "."],
                           cwd=staged, env=environment, check=True)
            if not (staged / "LICENSE").is_file():
                raise FileNotFoundError(staged / "LICENSE")
            stamp(staged, machin=pins["machin"], go_version=version,
                  compiler_sha256=sha256(executable), verification="git-revision-checked")
            require_new(source)
            require_new(binary)
            staged.rename(source)
            executable.rename(binary)


def stage_rust(source, destination, pins):
    source, destination = source.absolute(), destination.absolute()
    require_new(destination)
    manifest_name = "lib/rustlib/multirust-channel-manifest.toml"
    for name in (*RUST_NOTICES, "bin/rustc", "bin/cargo", manifest_name):
        if not (source / name).is_file():
            raise FileNotFoundError(source / name)
    manifest = tomllib.loads((source / manifest_name).read_text())
    if str(manifest["date"]) != pins["rust_nightly"].removeprefix("nightly-"):
        raise ValueError("Rust distribution date does not match pin")
    versions = {name: capture([str(source / "bin" / name), "--version"])
                for name in ("rustc", "cargo")}
    with tempfile.TemporaryDirectory(prefix="rust-stage-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "sysroot"
        (staged / "bin").mkdir(parents=True)
        shutil.copytree(source / "lib", staged / "lib", symlinks=True)
        for name in ("bin/rustc", "bin/cargo", *RUST_NOTICES):
            (staged / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, staged / name)
        stamp(staged, rust_nightly=pins["rust_nightly"], versions=versions,
              distribution_manifest_sha256=sha256(source / manifest_name),
              verification="distribution-date-checked")
        require_new(destination)
        staged.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pins_parser = commands.add_parser("pins")
    pins_parser.add_argument("--github-output", type=Path)
    commands.add_parser("sdk").add_argument("destination", type=Path)
    machin = commands.add_parser("machin")
    machin.add_argument("source", type=Path)
    machin.add_argument("binary", type=Path)
    rust = commands.add_parser("stage-rust")
    rust.add_argument("source", type=Path)
    rust.add_argument("destination", type=Path)
    args = parser.parse_args()
    pins = load_pins()
    if args.command == "pins":
        print(json.dumps(pins, indent=2))
        if args.github_output:
            with args.github_output.open("a") as output:
                for name in ("go_version", "rust_nightly", "rust_linker_probe"):
                    output.write(f"{name}={pins[name]}\n")
    elif args.command == "sdk":
        bootstrap_sdk(args.destination, pins)
    elif args.command == "machin":
        bootstrap_machin(args.source, args.binary, pins)
    else:
        stage_rust(args.source, args.destination, pins)


if __name__ == "__main__":
    main()
