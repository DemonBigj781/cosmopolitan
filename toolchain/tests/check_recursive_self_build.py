"""Build and validate byte-identical descendants from one self-building APE."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def zip_inventory(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"ZIP CRC validation failed: {path}")
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate ZIP resource path: {path}")
        inventory = tuple(
            (
                entry.filename,
                entry.CRC,
                entry.file_size,
                entry.compress_size,
                entry.compress_type,
                entry.external_attr,
                entry.date_time,
                entry.flag_bits,
                entry.create_system,
                entry.extract_version,
            )
            for entry in entries
        )
        manifest = hashlib.sha256(
            archive.read("self-build/manifest.json")
        ).hexdigest()
        metadata = hashlib.sha256(
            archive.read("toolchain-metadata/BUILD.json")
        ).hexdigest()
    return inventory, manifest, metadata


def load_provenance(artifact):
    sidecar = Path(str(artifact) + ".provenance.json")
    with sidecar.open(encoding="utf-8") as stream:
        return sidecar, json.load(stream)


def validate_provenance(artifact, parent, expected_generation, artifact_hash,
                        parent_hash, manifest_hash, metadata_hash):
    sidecar, record = load_provenance(artifact)
    expected = {
        "schema_version": 1,
        "generation": expected_generation,
        "parent_path": str(parent.resolve()),
        "parent_sha256": parent_hash,
        "artifact_sha256": artifact_hash,
        "artifact_size": artifact.stat().st_size,
        "self_build_manifest_sha256": manifest_hash,
        "sdk_metadata_sha256": metadata_hash,
        "target": "cosmopolitan-fat-ape-x86_64-aarch64",
    }
    if record != expected:
        raise ValueError(
            f"invalid provenance sidecar {sidecar}:\n"
            + json.dumps({"expected": expected, "actual": record}, indent=2)
        )


def run_ape(loader, artifact, arguments, scratch):
    environment = os.environ.copy()
    environment["COSMO_APE_LOADER"] = str(loader)
    environment["PATH"] = "/nonexistent"
    environment["TMPDIR"] = str(scratch)
    subprocess.run(
        [str(loader), str(artifact), *arguments],
        check=True,
        env=environment,
    )


def check_chain(mother, loader, destination, descendants):
    mother = mother.resolve()
    loader = loader.resolve()
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"chain destination must be empty: {destination}")
    scratch = destination / "scratch"
    scratch.mkdir()

    baseline_hash = sha256(mother)
    baseline_inventory, manifest_hash, metadata_hash = zip_inventory(mother)
    _, mother_record = load_provenance(mother)
    if mother_record.get("generation") != 0:
        raise ValueError("canonical mother provenance must be generation 0")
    bootstrap = Path(mother_record.get("parent_path", ""))
    if not bootstrap.is_file():
        raise ValueError(f"canonical mother bootstrap is unavailable: {bootstrap}")
    bootstrap_hash = sha256(bootstrap)
    validate_provenance(
        mother,
        bootstrap,
        0,
        baseline_hash,
        bootstrap_hash,
        manifest_hash,
        metadata_hash,
    )
    run_ape(loader, mother, ["selftest"], scratch)

    parent = mother
    parent_hash = baseline_hash
    produced = []
    for generation in range(1, descendants + 1):
        artifact = destination / f"generation-{generation}.com"
        run_ape(loader, parent, ["self-build", str(artifact)], scratch)
        run_ape(loader, artifact, ["selftest"], scratch)
        artifact_hash = sha256(artifact)
        if artifact_hash != baseline_hash:
            raise ValueError(
                f"generation {generation} is not byte-identical: "
                f"{artifact_hash} != {baseline_hash}"
            )
        inventory, child_manifest, child_metadata = zip_inventory(artifact)
        if inventory != baseline_inventory:
            raise ValueError(f"generation {generation} ZIP inventory differs")
        if child_manifest != manifest_hash or child_metadata != metadata_hash:
            raise ValueError(f"generation {generation} embedded metadata differs")
        validate_provenance(
            artifact,
            parent,
            generation,
            artifact_hash,
            parent_hash,
            manifest_hash,
            metadata_hash,
        )
        produced.append(artifact)
        parent = artifact
        parent_hash = artifact_hash

    print(
        "PASS: recursive self-build chain",
        f"descendants={descendants}",
        f"sha256={baseline_hash}",
        f"zip_entries={len(baseline_inventory)}",
    )
    for artifact in produced:
        print(artifact)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mother", type=Path, required=True)
    parser.add_argument("--ape-loader", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--descendants", type=int, default=2)
    args = parser.parse_args()
    if args.descendants < 2:
        parser.error("--descendants must include at least child and grandchild")
    try:
        check_chain(
            args.mother,
            args.ape_loader,
            args.destination,
            args.descendants,
        )
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
