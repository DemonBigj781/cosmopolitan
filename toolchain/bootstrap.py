"""Pinned CI bootstrap. Existing destinations are never trusted or overwritten."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile

from file_metadata import STAMP, sha256, tree_sha256

ROOT = Path(__file__).resolve().parents[1]
ELF_TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
RUST_HOSTS = (*ELF_TARGETS, "x86_64-apple-darwin", "aarch64-apple-darwin", "x86_64-pc-windows-msvc")
SCCACHE_RELEASE_HOSTS = RUST_HOSTS
NODEJS_RELEASE_HOSTS = RUST_HOSTS
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
    sccache = pins.get("sccache")
    if not isinstance(sccache, dict) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sccache.get("version", "")):
        raise ValueError("sccache must use an exact version")
    if sccache.get("repository") != "https://github.com/mozilla/sccache.git":
        raise ValueError("sccache repository must be the Mozilla upstream repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sccache.get("revision", "")):
        raise ValueError("sccache must use an exact upstream revision")
    assets = sccache.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(SCCACHE_RELEASE_HOSTS):
        raise ValueError("sccache assets must cover every packaged Rust host")
    for host, asset in assets.items():
        if not isinstance(asset, dict) or not re.fullmatch(r"sccache-v" + re.escape(sccache["version"]) + r"-[A-Za-z0-9._-]+\.tar\.gz", asset.get("archive", "")):
            raise ValueError(f"Invalid sccache archive for {host}")
        if not re.fullmatch(r"[0-9a-f]{64}", asset.get("sha256", "")):
            raise ValueError(f"Invalid sccache checksum for {host}")
    nodejs = pins.get("nodejs")
    if not isinstance(nodejs, dict) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", nodejs.get("version", "")):
        raise ValueError("Node.js must use an exact version")
    if nodejs.get("repository") != "https://github.com/nodejs/node.git":
        raise ValueError("Node.js repository must be the official upstream repository")
    license_pin = nodejs.get("license")
    expected_license_url = "https://raw.githubusercontent.com/nodejs/node/v{}/LICENSE".format(nodejs["version"])
    if (not isinstance(license_pin, dict) or license_pin.get("url") != expected_license_url or
            not re.fullmatch(r"[0-9a-f]{64}", license_pin.get("sha256", ""))):
        raise ValueError("Node.js license must be pinned to the exact upstream release tag")
    assets = nodejs.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(NODEJS_RELEASE_HOSTS):
        raise ValueError("Node.js assets must cover every packaged host")
    version = re.escape(nodejs["version"])
    for host, asset in assets.items():
        archive = asset.get("archive", "") if isinstance(asset, dict) else ""
        if not re.fullmatch(r"node-v" + version + r"-[A-Za-z0-9._-]+\.(?:tar\.xz|zip)", archive):
            raise ValueError(f"Invalid Node.js archive for {host}")
        if not re.fullmatch(r"[0-9a-f]{64}", asset.get("sha256", "")):
            raise ValueError(f"Invalid Node.js checksum for {host}")
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


def binary_host(path):
    with path.open("rb") as file:
        header = file.read(64)
        if len(header) < 20:
            raise ValueError(f"Invalid compiler host header: {path}")
        if header[:6] == b"\x7fELF\x02\x01":
            host = {62: ELF_TARGETS[0], 183: ELF_TARGETS[1]}.get(struct.unpack_from("<H", header, 18)[0])
        elif header[:4] == b"\xcf\xfa\xed\xfe":
            host = {0x01000007: RUST_HOSTS[2], 0x0100000c: RUST_HOSTS[3]}.get(struct.unpack_from("<I", header, 4)[0])
        elif len(header) == 64 and header[:2] == b"MZ":
            file.seek(struct.unpack_from("<I", header, 0x3c)[0])
            pe = file.read(6)
            host = RUST_HOSTS[4] if pe == b"PE\0\0\x64\x86" else None
        else:
            host = None
    if not host:
        raise ValueError(f"Unsupported compiler host format: {path}")
    return host


def merge_identical_tree(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        target = destination / path.name
        if path.is_symlink():
            if target.is_symlink() and os.readlink(path) == os.readlink(target):
                continue
            if target.exists() or target.is_symlink():
                raise ValueError(f"conflicting shared Rust target link: {path}")
            target.symlink_to(os.readlink(path))
        elif path.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(f"conflicting shared Rust target directory: {path}")
            merge_identical_tree(path, target)
        elif target.exists() or target.is_symlink():
            if (target.is_symlink() or not target.is_file() or sha256(path) != sha256(target) or
                    path.stat().st_mode & 0o777 != target.stat().st_mode & 0o777):
                raise ValueError(f"conflicting shared Rust target file: {path}")
        else:
            shutil.copy2(path, target)


def rust_hosts(root):
    value = json.loads((root / "hosts.json").read_text())
    hosts = value["hosts"]
    if value.get("schema_version") != 1 or not hosts or len(set(hosts)) != len(hosts) or any(h not in RUST_HOSTS for h in hosts):
        raise ValueError("Invalid Rust host inventory")
    if not set((*ELF_TARGETS, *hosts)) <= set(value["targets"]):
        raise ValueError("Missing required Rust targets")
    for host in hosts:
        suffix = ".exe" if "windows" in host else ""
        for executable in ("rustc", "cargo"):
            if binary_host(root / "hosts" / host / "sysroot/bin" / (executable + suffix)) != host:
                raise ValueError(f"Wrong compiler host in {host}")
        for notice in RUST_NOTICES:
            if not (root / "hosts" / host / "sysroot" / notice).is_file():
                raise FileNotFoundError(root / "hosts" / host / "sysroot" / notice)
        for target in value["targets"]:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", target):
                raise ValueError("Unsafe Rust target name")
            link = root / "hosts" / host / "sysroot/lib/rustlib" / target / "lib"
            if not link.is_symlink() or link.resolve(strict=True) != (root / "targets" / target / "lib").resolve(strict=True):
                raise ValueError(f"Invalid shared Rust target alias: {link}")
    return hosts


def stage_rust(sources, destination, pins):
    destination = destination.absolute()
    require_new(destination)
    if not sources or any(host not in RUST_HOSTS for host in sources):
        raise ValueError("Unsupported Rust compiler host selection")
    sources = {host: Path(sources[host]).absolute() for host in RUST_HOSTS if host in sources}
    manifest_name = "lib/rustlib/multirust-channel-manifest.toml"
    versions, manifests = {}, {}
    for host, source in sources.items():
        suffix = ".exe" if "windows" in host else ""
        for name in (*RUST_NOTICES, "bin/rustc" + suffix, "bin/cargo" + suffix, manifest_name):
            if not (source / name).is_file():
                raise FileNotFoundError(source / name)
        for name in ("rustc", "cargo"):
            if binary_host(source / "bin" / (name + suffix)) != host:
                raise ValueError(f"Incorrect compiler host: {host}")
        manifest = tomllib.loads((source / manifest_name).read_text())
        if str(manifest["date"]) != pins["rust_nightly"].removeprefix("nightly-"):
            raise ValueError("Rust distribution date does not match pin")
        versions[host] = {name: manifest["pkg"][name]["version"] for name in ("rustc", "cargo")}
        manifests[host] = sha256(source / manifest_name)
    with tempfile.TemporaryDirectory(prefix="rust-stage-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "sysroot"
        for host, source in sources.items():
            # rustc's DLL-based sysroot inference special-cases a directory named
            # exactly like its host triple. Keep the actual root one level below.
            host_root = staged / "hosts" / host / "sysroot"
            host_root.mkdir(parents=True)
            shutil.copytree(source / "bin", host_root / "bin", symlinks=True)
            rustlib = source / "lib/rustlib"
            def ignore_shared_libraries(directory, names):
                return ["lib"] if Path(directory).parent == rustlib and "lib" in names else []
            shutil.copytree(source / "lib", host_root / "lib", symlinks=True, ignore=ignore_shared_libraries)
            for target in sorted(rustlib.iterdir()):
                if (target / "lib").is_dir():
                    merge_identical_tree(target / "lib", staged / "targets" / target.name / "lib")
            for name in RUST_NOTICES:
                (host_root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, host_root / name)
        targets = sorted(p.name for p in (staged / "targets").iterdir())
        if not set((*ELF_TARGETS, *sources)) <= set(targets):
            raise ValueError("Missing native or cross-compilation Rust target libraries")
        for host in sources:
            for target in targets:
                link = staged / "hosts" / host / "sysroot/lib/rustlib" / target / "lib"
                link.parent.mkdir(parents=True, exist_ok=True)
                # A trailing slash preserves directory intent for Windows symlinkat.
                link.symlink_to(os.path.relpath(staged / "targets" / target / "lib", link.parent) + "/", target_is_directory=True)
        (staged / "hosts.json").write_text(json.dumps({"schema_version": 1, "hosts": list(sources),
            "targets": targets, "rust_nightly": pins["rust_nightly"]}, indent=2) + "\n")
        rust_hosts(staged)
        for path in staged.rglob("*"):
            if path.is_symlink() and not path.resolve(strict=True).is_relative_to(staged.resolve()):
                raise ValueError(f"Rust symlink escapes staged SDK: {path}")
        stamp(staged, rust_nightly=pins["rust_nightly"], versions=versions,
              distribution_manifest_sha256=manifests,
              verification="distribution-date-and-binary-host-checked")
        require_new(destination)
        staged.rename(destination)


def _archive_member(archive, suffix):
    with tarfile.open(archive, "r:gz") as bundle:
        matches = [member for member in bundle.getmembers()
                   if member.isfile() and member.name.endswith("/" + suffix)]
        if len(matches) != 1:
            raise ValueError(f"Expected one {suffix} in sccache archive")
        member = matches[0]
        if not member.name.startswith("sccache-v") or ".." in Path(member.name).parts:
            raise ValueError(f"Unsafe sccache archive member: {member.name}")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError(f"Cannot read sccache archive member: {member.name}")
        return stream.read()


def download_sccache_asset(asset, destination, root=ROOT):
    url = "https://github.com/mozilla/sccache/releases/download/v{}/{}".format(
        asset["version"], asset["archive"])
    subprocess.run(["curl", "-fsSL", url, "-o", str(destination)], cwd=root, check=True)
    if sha256(destination) != asset["sha256"]:
        raise ValueError(f"sccache archive checksum mismatch: {asset['archive']}")


def add_sccache(destination, pins, root=ROOT):
    destination = Path(destination).absolute()
    hosts = rust_hosts(destination)
    if not hosts:
        raise ValueError("sccache requires at least one staged Rust host")
    sccache = pins["sccache"]
    with tempfile.TemporaryDirectory(prefix="sccache-bootstrap-", dir=destination.parent) as temporary:
        for host in hosts:
            asset = dict(sccache["assets"][host], version=sccache["version"])
            archive = Path(temporary) / asset["archive"]
            download_sccache_asset(asset, archive, root)
            binary_name = "sccache.exe" if "windows" in host else "sccache"
            license_name = "LICENSE"
            binary = _archive_member(archive, binary_name)
            license_text = _archive_member(archive, license_name)
            bin_path = destination / "hosts" / host / "sysroot/bin" / binary_name
            license_path = destination / "hosts" / host / "sysroot/share/doc/sccache" / license_name
            if bin_path.exists() or license_path.exists():
                raise FileExistsError(f"Refusing to overwrite existing sccache files for {host}")
            bin_path.write_bytes(binary)
            bin_path.chmod(0o755)
            license_path.parent.mkdir(parents=True, exist_ok=True)
            license_path.write_bytes(license_text)
    rust_hosts(destination)
    stamp_path = destination / STAMP
    record = json.loads(stamp_path.read_text()) if stamp_path.exists() else {"schema_version": 1}
    record["sccache"] = {
        "repository": sccache["repository"],
        "revision": sccache["revision"],
        "version": sccache["version"],
        "assets": {host: sccache["assets"][host] for host in SCCACHE_RELEASE_HOSTS},
        "verification": "release-archive-sha256-checked",
    }
    record["tree_sha256"] = tree_sha256(destination)
    stamp_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def _nodejs_archive_member(archive, suffix):
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            matches = [member for member in bundle.infolist()
                       if not member.is_dir() and member.filename.endswith("/" + suffix)]
            if len(matches) != 1:
                raise ValueError(f"Expected one {suffix} in Node.js archive")
            member = matches[0]
            if member.filename.startswith("/") or ".." in Path(member.filename).parts:
                raise ValueError(f"Unsafe Node.js archive member: {member.filename}")
            return bundle.read(member)
    with tarfile.open(archive, "r:xz") as bundle:
        matches = [member for member in bundle.getmembers()
                   if member.isfile() and member.name.endswith("/" + suffix)]
        if len(matches) != 1:
            raise ValueError(f"Expected one {suffix} in Node.js archive")
        member = matches[0]
        if member.name.startswith("/") or ".." in Path(member.name).parts:
            raise ValueError(f"Unsafe Node.js archive member: {member.name}")
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError(f"Cannot read Node.js archive member: {member.name}")
        return stream.read()


def download_nodejs_asset(asset, destination, root=ROOT):
    url = "https://nodejs.org/dist/v{}/{}".format(asset["version"], asset["archive"])
    subprocess.run(["curl", "-fsSL", url, "-o", str(destination)], cwd=root, check=True)
    if sha256(destination) != asset["sha256"]:
        raise ValueError(f"Node.js archive checksum mismatch: {asset['archive']}")


def download_nodejs_license(license_pin, destination, root=ROOT):
    subprocess.run(["curl", "-fsSL", license_pin["url"], "-o", str(destination)], cwd=root, check=True)
    if sha256(destination) != license_pin["sha256"]:
        raise ValueError("Node.js license checksum mismatch")


def nodejs_hosts(root):
    value = json.loads((root / "hosts.json").read_text())
    hosts = value.get("hosts")
    if (value.get("schema_version") != 1 or not hosts or len(set(hosts)) != len(hosts) or
            any(host not in NODEJS_RELEASE_HOSTS for host in hosts)):
        raise ValueError("Invalid Node.js host inventory")
    for host in hosts:
        suffix = ".exe" if "windows" in host else ""
        binary = root / "hosts" / host / "bin" / ("node" + suffix)
        if binary_host(binary) != host:
            raise ValueError(f"Wrong Node.js host in {host}")
        notice = root / "hosts" / host / "share/doc/nodejs/LICENSE"
        if not notice.is_file():
            raise FileNotFoundError(notice)
    return hosts


def bootstrap_nodejs(destination, pins, root=ROOT):
    destination = destination.absolute()
    require_new(destination)
    nodejs = pins["nodejs"]
    with tempfile.TemporaryDirectory(prefix="nodejs-bootstrap-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "nodejs"
        license_path = Path(temporary) / "LICENSE"
        download_nodejs_license(nodejs["license"], license_path, root)
        for host in NODEJS_RELEASE_HOSTS:
            asset = dict(nodejs["assets"][host], version=nodejs["version"])
            archive = Path(temporary) / asset["archive"]
            download_nodejs_asset(asset, archive, root)
            suffix = ".exe" if "windows" in host else ""
            binary = staged / "hosts" / host / "bin" / ("node" + suffix)
            notice = staged / "hosts" / host / "share/doc/nodejs/LICENSE"
            binary.parent.mkdir(parents=True, exist_ok=True)
            notice.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(_nodejs_archive_member(archive, "node.exe" if suffix else "bin/node"))
            binary.chmod(0o755)
            shutil.copy2(license_path, notice)
        (staged / "hosts.json").write_text(json.dumps({
            "schema_version": 1,
            "hosts": list(NODEJS_RELEASE_HOSTS),
            "version": nodejs["version"],
        }, indent=2) + "\n")
        nodejs_hosts(staged)
        stamp(staged, nodejs={
            "repository": nodejs["repository"],
            "version": nodejs["version"],
            "assets": nodejs["assets"],
        }, verification="release-archive-sha256-and-binary-host-checked")
        require_new(destination)
        staged.rename(destination)


def bootstrap_rust(destination, pins):
    destination = destination.absolute()
    require_new(destination)
    rustup = shutil.which("rustup")
    if not rustup:
        raise FileNotFoundError("rustup is required to install the pinned Rust distributions")
    with tempfile.TemporaryDirectory(prefix="rust-install-", dir=destination.parent) as temporary:
        root = Path(temporary)
        environment = dict(os.environ, RUSTUP_HOME=str(root / "rustup"), CARGO_HOME=str(root / "cargo"))
        sources = {}
        for host in RUST_HOSTS:
            name = pins["rust_nightly"] + "-" + host
            subprocess.run([rustup, "toolchain", "install", name, "--profile", "minimal",
                            "--no-self-update", "--force-non-host"], env=environment, check=True)
            sources[host] = root / "rustup/toolchains" / name
        stage_rust(sources, destination, pins)
    add_sccache(destination, pins)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pins_parser = commands.add_parser("pins")
    pins_parser.add_argument("--github-output", type=Path)
    commands.add_parser("sdk").add_argument("destination", type=Path)
    commands.add_parser("rust").add_argument("destination", type=Path)
    commands.add_parser("nodejs").add_argument("destination", type=Path)
    machin = commands.add_parser("machin")
    machin.add_argument("source", type=Path)
    machin.add_argument("binary", type=Path)
    rust = commands.add_parser("stage-rust")
    rust.add_argument("source", type=Path)
    rust.add_argument("destination", type=Path)
    sccache = commands.add_parser("add-sccache")
    sccache.add_argument("destination", type=Path)
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
    elif args.command == "rust":
        bootstrap_rust(args.destination, pins)
    elif args.command == "nodejs":
        bootstrap_nodejs(args.destination, pins)
    elif args.command == "add-sccache":
        add_sccache(args.destination, pins)
    else:
        compiler = args.source / "bin/rustc"
        if not compiler.is_file():
            compiler = compiler.with_suffix(".exe")
        stage_rust({binary_host(compiler): args.source}, args.destination, pins)


if __name__ == "__main__":
    main()
