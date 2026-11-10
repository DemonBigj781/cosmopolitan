#!/usr/bin/env python3
"""Collect source license notices alongside the experimental binary."""
import json
from pathlib import Path
import shutil
import tomllib

from source_receipt import tree_receipt, verify_prepared

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
OUT = REPO / "o/webgpu"
DEST = OUT / "licenses"


def collect(name, root):
    destination = DEST / name
    destination.mkdir(parents=True, exist_ok=True)
    paths = set()
    for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "COPYRIGHT*"):
        paths.update(root.glob(pattern))
    for path in sorted(paths):
        if path.is_file():
            shutil.copyfile(path, destination / path.name)


def main():
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    prepared, _ = verify_prepared(OUT, ROOT)
    # Exact caller patch bytes and the manifest are provenance, not a new
    # license grant. Keep their embedded notices and upstream licenses intact.
    shutil.copytree(OUT / prepared["application_patch_provenance_path"],
                    DEST / "application-patches")
    if tree_receipt(DEST / "application-patches") != prepared["application_patch_provenance"]:
        raise ValueError("Application patch provenance copy differs from prepared input")
    collect("cosmopolitan", REPO)
    collect("wgpu-native", ROOT / "upstream")
    collect("webgpu-headers", ROOT / "upstream/ffi/webgpu-headers")
    collect("rust-ape", REPO / "third_party/rust_ape/upstream")
    records = {}
    # This list includes the Rust target libraries and host build tools emitted
    # by Cargo. Retaining the latter's notices is harmless and makes the source
    # provenance complete; they are not separate runtime payloads.
    for line in (OUT / "cargo-artifacts.jsonl").read_text().splitlines():
        try:
            artifact = json.loads(line)
        except json.JSONDecodeError:
            continue
        if artifact.get("reason") != "compiler-artifact":
            continue
        manifest = Path(artifact["manifest_path"])
        metadata = tomllib.loads(manifest.read_text())
        package = metadata["package"]
        key = f"{package['name']}-{package['version']}"
        records[key] = {"package": artifact["package_id"],
                        "license": package.get("license", "see source notice")}
        collect(key, manifest.parent)
        if "license-file" in package:
            path = manifest.parent / package["license-file"]
            if path.is_file():
                shutil.copyfile(path, DEST / key / path.name)
    rust_docs = (REPO / "o/rust-ape/toolchain/rustup/toolchains/"
                 "nightly-2026-07-28-x86_64-unknown-linux-gnu/share/doc/rust")
    if rust_docs.is_dir():
        collect("rust-standard-library", rust_docs)
    (DEST / "DEPENDENCIES.json").write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    (OUT / "NOTICE.txt").write_text(
        "Experimental Cosmopolitan WebGPU executable.\n"
        "Source: https://github.com/DemonBigj781/cosmopolitan-lua/tree/webgpu\n"
        "Retain the licenses/ directory when redistributing.\n"
        "licenses/application-patches/ retains exact applied application patch provenance.\n"
        "That provenance is not a license grant; retain any additional patch author notices.\n"
        "DEPENDENCIES.json also records host build tools; those tools are not runtime payloads.\n"
    )
    print(f"Collected license notices for {len(records)} Cargo packages")


if __name__ == "__main__":
    main()
