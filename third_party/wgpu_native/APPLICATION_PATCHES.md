# Explicit application patches and build receipts

The default build has no application patches. A consumer that needs an extra
wgpu-native API must explicitly provide its own patch bytes and expected hashes.
The foundation does not embed an application's extension or infer patch identity
from exported symbols. Foundation port patches still apply first. The pristine import must match the
complete declared file set and hashes, including the separate WebGPU header
import. Unlisted files, symlinks and special files are rejected before copying
and before patching the copied tree.

## Manifest

Set `WGPU_APPLICATION_PATCH_MANIFEST` when invoking `build.sh`, or use
`prepare.py --application-patch-manifest PATH` for preparation alone. The CLI
option takes precedence over the environment variable. The JSON schema is:

```json
{
  "schema_version": 1,
  "patches": [
    {
      "name": "backend/patches/application-extension.patch",
      "path": "patches/application-extension.patch",
      "sha256": "<64 lowercase hexadecimal characters>"
    }
  ]
}
```

`patches` is ordered. Names must be unique, normalized relative POSIX paths
using letters, digits, `_`, `-`, `.`, `+` and `/`. Empty components, `.` and `..`
components, duplicate names and file/directory name collisions are rejected.
`path` can be absolute, or relative to the manifest file's directory. The
manifest and patch paths must be regular files without symlink components.
Unknown schema fields and duplicate JSON keys are rejected.

Every patch is read and SHA-256 checked before any build source is deleted,
copied, downloaded or patched. Exact validated bytes are copied into a private
temporary directory and applied from there, so editing the original afterward
cannot substitute a different patch. Patches target the `wgpu-native` source
root under `o/webgpu/source`, after the foundation patches and reviewed lockfile
are installed. They are applied in order with `patch --batch --forward --fuzz=0
-p1`. A failure aborts preparation. No patch is silently skipped or reversed.
This input is executable build-source material: only use reviewed patches from
a trusted source. Hash validation establishes identity, not safety or licensing.

## Preparation identity

A successful preparation writes `o/webgpu/PREPARED.json` with:

- `application_patches`: exact normalized name-to-SHA-256 mapping.
- `application_patch_order`: the original application order, kept separately
  from JSON object ordering.
- `application_patch_manifest_sha256`: original manifest hash, or null for the
  default absent manifest.
- `source_trees`: every path, file hash, entry type and permission mode in the
  prepared native source and patched ash/wgpu-hal directories.
- `prepared_source_sha256`: SHA-256 of the canonical `source_trees` JSON object.
  Each tree's own `sha256` hashes its canonical `entries` object. Canonical JSON
  is Python `json.dumps(sort_keys=True, separators=(",", ":"),
  ensure_ascii=True).encode()` with no trailing newline.
- `foundation_inputs`: exact hashes of the pinned import manifest, reviewed
  lockfile, foundation port patches and preparation/generator helpers.
- `foundation_pins`: the full immutable import manifest, and `crate_pins` for
  the exact ash/wgpu-hal version and source archive hashes, plus
  `prepared_lock_sha256` for the lockfile actually used by the patched source.
- `generated_inputs`: hashes of the generated Cargo patch config and Vulkan
  dispatch signatures.
- `application_patch_provenance_path`: `application-patches`, containing exact
  applied bytes, their normalized `MANIFEST.json`, and `INPUT_MANIFEST.json`
  when supplied. `application_patch_provenance` binds that whole tree.

`--bootstrap-lock` remains a maintainer-only preparation mode. Its receipt
cannot authorize a normal pinned build. No successful receipt is written if
preparation fails after source materialization starts.

## Build identity

The build verifies the complete prepared receipt immediately before invoking
Cargo and again after linking, against the same captured receipt SHA-256.
Changed source, dependencies, patches, config, generated signatures or receipt
abort the build. `BUILD.json` gets its patch map only from this verified receipt,
not from a caller-supplied label for an existing archive. C compilation also uses
the prepared FFI headers instead of the pristine import.

The optional `COSMO_RUST_CARGO_RECEIPT` mechanism in the Rust wrapper captures
actual expanded Cargo arguments, the selected target's hash, generated config
hashes, runtime identity and a whitelist of effective build environment flags
immediately before `exec cargo`. WebGPU owns its `cargo-invocation.json` path.
The wrapper receipt is explicitly `status: "invocation-only"`; it is not a build
success claim. Cargo's JSON artifact log must report a successful build and the
actual selected `wgpu_native` static library, even for a cache hit.

After a successful link, `BUILD.json` records:

- `prepared_receipt_sha256`, `prepared_source_sha256`, `prepared_lock_sha256`,
  `foundation_inputs`, `application_patches` and `application_patch_order`.
- `cargo_invocation_sha256` and `cargo_artifacts_sha256` for the exact evidence
  files, and `build_recipe` with the actual Cargo, C linker, APE packager and PE
  configuration arguments. `build-commands.json` preserves those latter arrays.
- `rust_target` and `rust_target_spec_sha256` for the selected custom target.
- `rust_std_delivery: {"mode": "embedded-in-wgpu", "container_sha256": "..."}`
  where the container hash is the selected wgpu static archive. This declares
  the delivery container, not which standard-library members survive codegen,
  extraction or final dead-code elimination. It does not claim runtime tests
  passed or that separately delivered Rust standard-library archives exist.

The existing runtime binding, ordinary static-archive closure checks and
compile-only probe status remain in force. Application execution is not implied
by a successful build. These local hash receipts are consistency evidence, not
signed attestations against a malicious build host.

## Redistribution and validation

`package_notices.py` retains exact application patch provenance separately at
`licenses/application-patches/`, recorded by `BUILD.application_patch_licenses_path`.
The existing upstream notices are unchanged. Patch provenance is not a license
grant: the supplying application remains responsible for any extra required
license texts and author notices.

`toolchain/webgpu_sdk.py` stages the exact prepared C headers for source-runtime
and receipted/application-patched builds. It verifies the same preparation
receipt before and after staging, checks header bytes while copying, preserves
prepared author/license notices and exact BUILD/PREPARED records, and records
those hashes in the relocatable SDK manifest. Legacy stock inputs without
receipt fields retain their previous staging behavior.

Focused tests are in `tests/test_source_receipt.py`, `tests/test_build_receipt.py`
and `toolchain/tests/test_webgpu_sdk.py`. They use local fixtures and mocks, with
no Cargo, network, compiler or downloaded source requirements:

```sh
python3 -m unittest discover -s third_party/wgpu_native/tests -p 'test_*receipt.py'
python3 -m unittest discover -s toolchain/tests -p 'test_webgpu_sdk.py'
```

The 30 receipt tests and 15 staging tests passed during this implementation
pass. No real source preparation, dependency download or SDK build was run.
A real source-profile build and platform execution remain separate validation
steps; fixture checks do not establish CPU/GPU model inference or performance.
