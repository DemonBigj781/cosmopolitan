"""Attach an SDK using Cosmopolitan zipcopy, preserving existing APE resources."""

import argparse
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

MAX_NATIVE_ARCHIVE_SIZE = 2**31 - 1  # Current zipcopy / ZIPOS ZIP32 contract.

def inventory(archive):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("duplicate embedded resource paths")
    return {entry.filename: (entry.CRC, entry.file_size, entry.external_attr,
                             entry.compress_type) for entry in entries}


def attach_sdk(executable, sdk_archive, output, zipcopy):
    if output.resolve() in (executable.resolve(), sdk_archive.resolve()):
        raise ValueError("output must not replace an input")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Work beside the destination so the final rename is atomic. Inputs and the
    # previous output remain intact if attachment or validation fails.
    with tempfile.TemporaryDirectory(prefix="attach-sdk-", dir=output.parent) as work:
        merged = Path(work) / "resources.zip"
        staged = Path(work) / "toolchain.com"
        shutil.copyfile(sdk_archive, merged)
        with zipfile.ZipFile(merged, "a", allowZip64=False) as resources:
            existing = inventory(resources)
            if zipfile.is_zipfile(executable):
                with zipfile.ZipFile(executable) as original:
                    original_names = inventory(original)
                    if existing.keys() & original_names.keys():
                        raise ValueError("duplicate resource path between APE and SDK")
                    for entry in original.infolist():
                        with original.open(entry) as source:
                            with resources.open(copy.copy(entry), "w") as target:
                                shutil.copyfileobj(source, target, 1024 * 1024)
            expected = inventory(resources)
        if executable.stat().st_size + merged.stat().st_size > MAX_NATIVE_ARCHIVE_SIZE:
            raise ValueError("Combined APE exceeds the current 2 GiB native ZIP limit")
        shutil.copyfile(executable, staged)
        # The SDK's APE utility can bootstrap through sh without a global APE
        # handler. This is a build-time tool, never a deployed runtime dependency.
        subprocess.run(["sh", str(zipcopy), str(merged), str(staged)], check=True)
        with zipfile.ZipFile(staged) as attached:
            if inventory(attached) != expected:
                raise ValueError("attached ZIP inventory differs from merged resources")
            if attached.testzip() is not None:
                raise ValueError("attached ZIP failed its CRC check")
        with executable.open("rb") as original, staged.open("rb") as combined:
            while block := original.read(1024 * 1024):
                if combined.read(len(block)) != block:
                    raise ValueError("attachment changed the executable prefix")
        staged.chmod(executable.stat().st_mode & 0o777)
        os.replace(staged, output)
    print(f"PASS: native ZIP SDK attached; {len(expected)} resources preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zipcopy", type=Path, required=True)
    args = parser.parse_args()
    attach_sdk(args.executable, args.sdk, args.output, args.zipcopy)


if __name__ == "__main__":
    main()
