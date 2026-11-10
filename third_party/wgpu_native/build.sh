#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build one x86-64 APE: C application + Rust WebGPU + Cosmopolitan runtime.
set -euo pipefail
webgpu_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
webgpu_repo=$(cd -- "$webgpu_dir/../.." && pwd)
webgpu_out="$webgpu_repo/o/webgpu"
webgpu_rust="$webgpu_repo/third_party/rust_ape/run.sh"
webgpu_sdk="$webgpu_repo/o/rust-ape/sdk"
webgpu_software=0
webgpu_compile_only=0
webgpu_software_args=()
webgpu_lavapipe_args=()

while (($#)); do
  case "$1" in
    --help|-h)
      cat <<'EOF'
Usage: bash third_party/wgpu_native/build.sh [--software] [--compile-only]

Build on Linux x86-64. Required host tools: a C compiler, libclang (for
bindgen), Python 3.12+, curl, git, patch, tar, unzip, sha256sum.
The pinned Rust/Cosmocc SDK and build outputs live under o/.

Output: o/webgpu/webgpu_compute (one Windows/Linux x86-64 APE)
        o/webgpu/ape-x86_64.elf (optional explicit Linux APE loader)

--software also builds Mesa lavapipe and LLVM as Cosmopolitan static libraries.
This mode additionally needs a native C++ compiler, CMake 3.24+, Ninja, and
Python venv support. The resulting executable runs --software without a host
Vulkan loader or driver. Its Mesa outputs use the default o/lavapipe directory.

--compile-only skips the native ABI/loader behavior tests and passes --no-run
to Mesa and LLVM. The application is built, but workloads are not verified.
COSMO_RUNTIME_PROFILE=source-built requires COSMO_RUNTIME_SDK to name a complete
source-built SDK. Start with a fresh default o/rust-ape tree for that profile.
COSMOCC and COSMO, if set, must name o/rust-ape/sdk/vendor/cosmocc after setup.
WGPU_APPLICATION_PATCH_MANIFEST optionally names an ordered SHA-256-checked
application patch manifest; see APPLICATION_PATCHES.md. No patches are assumed.

Run --version to check C/Rust linkage without Vulkan. Run without arguments
for shader dispatch/readback using a host Vulkan loader and driver. Use
--software --matmul to test the embedded CPU driver with f32 matrix operations.
Use --software --raster to draw and read back an offscreen RGBA8 triangle.
This is an experimental headless compute/raster port, not a window system.
EOF
      exit 0 ;;
    --software) webgpu_software=1 ;;
    --compile-only) webgpu_compile_only=1; webgpu_lavapipe_args=(--no-run) ;;
    *) echo 'Unknown argument; see --help.' >&2; exit 2 ;;
  esac
  shift
done
cd -- "$webgpu_repo"
if [[ -n ${COSMO_RUST_BUILD_ROOT:-} || -n ${COSMO_RUST_ARCH:-} ]]; then
  echo 'This WebGPU target uses the default o/rust-ape SDK and x86-64 architecture.' >&2
  exit 2
fi
if ((webgpu_software)) && [[ -n ${LAVAPIPE_OUT:-} ]]; then
  echo 'This WebGPU target uses the default o/lavapipe directory; unset LAVAPIPE_OUT.' >&2
  exit 2
fi
mkdir -p -- "$webgpu_out"
# A failed rebuild must not retain a success receipt from an older archive.
rm -f -- "$webgpu_out/BUILD.json" "$webgpu_out/SHA256SUMS" \
  "$webgpu_out/build-commands.json" "$webgpu_out/cargo-invocation.json"
webgpu_build_script_sha256=$(sha256sum "$webgpu_dir/build.sh" | cut -d ' ' -f1)
webgpu_receipt_helper_sha256=$(sha256sum "$webgpu_dir/build_receipt.py" | cut -d ' ' -f1)
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2}
export WGPU_NATIVE_VERSION=v29.0.1.1

bash "$webgpu_rust" setup
webgpu_runtime=$(python3 - "$webgpu_repo/toolchain" "$webgpu_sdk/vendor/cosmocc" "${COSMOCC:-}" "${COSMO:-}" <<'PY'
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from runtime_identity import selected_runtime

sdk = Path(sys.argv[2]).resolve(strict=True)
for name, override in zip(("COSMOCC", "COSMO"), sys.argv[3:5]):
    if override and Path(override).resolve(strict=True) != sdk:
        raise SystemExit(name + " must match the SDK bound to Rust: " + str(sdk))
print(json.dumps(selected_runtime(sdk), sort_keys=True))
PY
)
export COSMOCC="$webgpu_sdk/vendor/cosmocc"
export COSMO="$COSMOCC"
if ((!webgpu_compile_only)); then
  bash "$webgpu_dir/tests/run_abi.sh"
  bash "$webgpu_dir/tests/run_loader.sh"
fi
if ((webgpu_software)); then
  bash "$webgpu_repo/third_party/lavapipe/build.sh" "${webgpu_lavapipe_args[@]}"
fi
python3 "$webgpu_dir/prepare.py"
# Bindgen must read the same C scalar definitions as the Cosmopolitan compiler.
# Its default Linux target would otherwise discover the build host's headers.
export BINDGEN_EXTRA_CLANG_ARGS="-I\"$webgpu_sdk/vendor/cosmocc/include\" -include \"$webgpu_sdk/vendor/cosmocc/include/libc/normalize.inc\" ${BINDGEN_EXTRA_CLANG_ARGS:-}"
webgpu_prepared_sha256=$(python3 "$webgpu_dir/source_receipt.py" --output "$webgpu_out")
# The wrapper records the expanded Cargo argv and its whitelisted environment
# just before exec. Never accept a caller-provided invocation receipt path.
COSMO_RUST_CARGO_RECEIPT="$webgpu_out/cargo-invocation.json" \
  bash "$webgpu_rust" cargo --config "$webgpu_out/cargo.cosmo.toml" \
    build --manifest-path "$webgpu_out/source/Cargo.toml" --locked \
    --release --lib --no-default-features --features wgsl,vulkan \
    --message-format=json-render-diagnostics > "$webgpu_out/cargo-artifacts.jsonl"

webgpu_archive="$webgpu_repo/o/rust-ape/build/x86_64-unknown-linux-musl/release/libwgpu_native.a"
test -s "$webgpu_archive"
webgpu_cargo_evidence=$(python3 "$webgpu_dir/build_receipt.py" verify-cargo \
  "$webgpu_out" "$webgpu_sdk" "$webgpu_runtime" "$webgpu_archive")
read -r webgpu_cargo_invocation_sha256 webgpu_cargo_artifacts_sha256 <<< "$webgpu_cargo_evidence"
webgpu_archive_sha256=$(sha256sum "$webgpu_archive" | cut -d ' ' -f1)
if [[ ${COSMO_RUNTIME_PROFILE:-stock-sdk} == source-built ]]; then
  python3 - "$webgpu_repo/toolchain" "$webgpu_archive" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from static_archives import archive_sha256

archive_sha256(sys.argv[2])
PY
fi
# Validate the actual archive closure immediately before the final link.
if ((webgpu_software)); then
  python3 - "$webgpu_repo/o/lavapipe/LINK.json" "$webgpu_runtime" \
    "$webgpu_repo/toolchain" "$webgpu_out/software-link-manifest.json" > "$webgpu_out/software-link-args" <<'PY'
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[3])
from static_archives import verify_archives

manifest = json.loads(Path(sys.argv[1]).read_text())
runtime = json.loads(sys.argv[2])
if manifest.get("runtime", {"kind": "stock-sdk"}) != runtime:
    raise SystemExit("Mesa runtime identity does not match the SDK bound to Rust")
args = manifest["link_args"]
if not isinstance(args, list) or not args or any(
        not isinstance(arg, str) or not arg or "\0" in arg or "\n" in arg for arg in args):
    raise SystemExit("Invalid static lavapipe link arguments")
archives = [arg for arg in args if not arg.startswith("-")]
if not archives or any(not Path(arg).is_absolute() or Path(arg).suffix != ".a" for arg in archives):
    raise SystemExit("Expected absolute static lavapipe archive inputs")
verify_archives(archives, manifest.get("link_archive_sha256"),
                required=runtime["kind"] != "stock-sdk")
Path(sys.argv[4]).write_text(json.dumps(manifest, sort_keys=True) + "\n")
for arg in args:
    if arg.endswith((".so", ".dll", ".dylib")) or ".so." in arg:
        raise SystemExit(f"A software Vulkan link input is a shared library: {arg}")
    sys.stdout.buffer.write(arg.encode() + b"\0")
PY
  mapfile -d '' -t webgpu_software_args < "$webgpu_out/software-link-args"
  webgpu_software_args=(-DCOSMO_WGPU_EMBEDDED_LAVAPIPE=1 "${webgpu_software_args[@]}")
fi
# PTHREAD_MUTEX_INITIALIZER in this Cosmocc release initializes trailing
# private fields implicitly; -Wextra otherwise warns about the SDK macro.
webgpu_link_command=("$webgpu_sdk/generated/linker-x86_64.bash" \
  -mcosmo -std=c11 -O2 -g -fno-omit-frame-pointer -fno-stack-protector \
  -Wall -Wextra -Werror -Wno-missing-field-initializers \
  -I"$webgpu_out/source/ffi" \
  -I"$webgpu_out/source/ffi/webgpu-headers" \
  -I"$webgpu_dir/runtime" -I"$webgpu_out/generated" \
  "$webgpu_dir/examples/compute.c" \
  "$webgpu_dir/runtime/vulkan_loader.c" \
  "$webgpu_dir/runtime/win64_bridge.c" \
  "$webgpu_archive" "${webgpu_software_args[@]}" \
  -Wl,--gc-sections -pthread -lm -ldl \
  -o "$webgpu_out/webgpu_compute.dbg")
python3 "$webgpu_dir/build_receipt.py" record-command "$webgpu_out/build-commands.json" \
  link "${webgpu_link_command[@]}"
"${webgpu_link_command[@]}"

# Keep APE/PE support. The Linux-only apelink -V1 option is not used.
webgpu_apelink_command=(sh "$webgpu_sdk/vendor/cosmocc/bin/apelink" \
  -l "$webgpu_sdk/vendor/cosmocc/bin/ape-x86_64.elf" \
  -o "$webgpu_out/webgpu_compute" "$webgpu_out/webgpu_compute.dbg")
python3 "$webgpu_dir/build_receipt.py" record-command "$webgpu_out/build-commands.json" \
  apelink "${webgpu_apelink_command[@]}"
"${webgpu_apelink_command[@]}"
webgpu_pe_command=(python3 "$webgpu_dir/configure_pe.py" "$webgpu_out/webgpu_compute")
python3 "$webgpu_dir/build_receipt.py" record-command "$webgpu_out/build-commands.json" \
  configure_pe "${webgpu_pe_command[@]}"
"${webgpu_pe_command[@]}"
webgpu_commands_sha256=$(sha256sum "$webgpu_out/build-commands.json" | cut -d ' ' -f1)
cp -- "$webgpu_sdk/vendor/cosmocc/bin/ape-x86_64.elf" "$webgpu_out/ape-x86_64.elf"
chmod +x -- "$webgpu_out/webgpu_compute" "$webgpu_out/ape-x86_64.elf"

python3 "$webgpu_dir/package_notices.py"
python3 - "$webgpu_out" "$webgpu_dir/Cargo.cosmo.lock" "$webgpu_software" \
  "$webgpu_compile_only" "$webgpu_runtime" "$webgpu_archive" "$webgpu_sdk" \
  "$webgpu_prepared_sha256" "$webgpu_cargo_invocation_sha256" "$webgpu_cargo_artifacts_sha256" \
  "$webgpu_archive_sha256" "$webgpu_commands_sha256" "$webgpu_build_script_sha256" \
  "$webgpu_receipt_helper_sha256" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shutil

out, lock = map(Path, sys.argv[1:3])
software = sys.argv[3] == "1"
compile_only = sys.argv[4] == "1"
runtime = json.loads(sys.argv[5])
archive, sdk = map(Path, sys.argv[6:8])
prepared_sha, invocation_sha, artifacts_sha, archive_sha, commands_sha, script_sha, helper_sha = sys.argv[8:15]
foundation = out.parent.parent / "third_party/wgpu_native"
sys.path.insert(0, str(foundation))
from source_receipt import file_sha256, tree_receipt, verify_prepared
from build_receipt import verify_cargo

prepared, _ = verify_prepared(out, foundation, prepared_sha)
cargo_evidence = verify_cargo(out, sdk, runtime, archive, invocation_sha, artifacts_sha)
for path, expected in ((archive, archive_sha), (out / "build-commands.json", commands_sha),
                       (foundation / "build.sh", script_sha), (foundation / "build_receipt.py", helper_sha)):
    if file_sha256(path) != expected:
        raise SystemExit("WebGPU build input or evidence changed during the link: " + str(path))
if tree_receipt(out / "licenses/application-patches") != prepared["application_patch_provenance"]:
    raise SystemExit("Packaged application patch provenance does not match the preparation")
sys.path.insert(0, str(out.parent.parent / "toolchain"))
from runtime_identity import selected_runtime
from static_archives import archive_sha256, verify_archives

if selected_runtime(sdk / "vendor/cosmocc") != runtime:
    raise SystemExit("The selected runtime changed during the WebGPU build")
if runtime["kind"] != "stock-sdk":
    sys.path.insert(0, str(out.parent.parent / "third_party/rust_ape"))
    from runtime_profile import expected_shim_objects, verify_binding
    verify_binding(sdk)
    shims = expected_shim_objects(sdk)
else:
    shims = sorted((sdk / "generated").glob("shim-*-x86_64.o"))
manifest = {
    "target": "x86_64 Cosmopolitan APE",
    "wgpu_native": "v29.0.1.1",
    "wgpu_revision": "6aed50955d934ac36049ba8d002034841633ae02",
    "rust_ape_revision": "0e2d0a89f4c089c7f21377a9c7ce51a6e82c2791",
    "cosmocc": "4.0.2" if runtime["kind"] == "stock-sdk" else "source-built",
    "runtime": runtime,
    "rust": "nightly-2026-07-28",
    "rust_target": "x86_64-unknown-linux-musl",
    "rust_target_spec_sha256": cargo_evidence["cargo_invocation"]["target_sha256"],
    "features": ["wgsl", "vulkan"],
    "application_patches": prepared["application_patches"],
    "application_patch_order": prepared["application_patch_order"],
    "application_patch_licenses_path": "licenses/application-patches",
    "prepared_receipt_sha256": prepared_sha,
    "prepared_source_sha256": prepared["prepared_source_sha256"],
    "prepared_lock_sha256": prepared["prepared_lock_sha256"],
    "foundation_inputs": prepared["foundation_inputs"],
    "cargo_artifacts_sha256": artifacts_sha,
    "cargo_invocation_sha256": invocation_sha,
    "build_recipe": {
        "cargo": cargo_evidence["cargo_invocation"],
        "commands": json.loads((out / "build-commands.json").read_text()),
        "commands_sha256": commands_sha,
        "build_script_sha256": script_sha,
        "receipt_helper_sha256": helper_sha,
    },
    # This names the delivery container, not optimizer/codegen behavior. It
    # does not claim that each libstd object survives archive extraction/GC.
    "rust_std_delivery": {"mode": "embedded-in-wgpu", "container_sha256": archive_sha},
    "build_status": "built",
    "compile_only": compile_only,
    "probes": {
        "native_abi": "not-run" if compile_only else "passed",
        "native_loader": "not-run" if compile_only else "passed",
        "application": "not-run",
    },
    "supported_workloads": ["collatz", "f32-matmul", "rgba8-triangle-raster"],
    "verified_workloads": [],
    "embedded_software_vulkan": software,
    "windows_native_stack_reserve": 8 * 1024 * 1024,
    "windows_native_stack_commit": 4096,
    "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    "application_sha256": hashlib.sha256((out / "webgpu_compute").read_bytes()).hexdigest(),
    "wgpu_archive_sha256": (archive_sha256(archive) if runtime["kind"] != "stock-sdk"
                            else file_sha256(archive)),
    "rust_shim_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in shims},
    "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "source_dirty": bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], text=True)),
}
if software:
    driver = out.parent / "lavapipe"
    manifest["software_vulkan"] = json.loads((out / "software-link-manifest.json").read_text())
    if json.loads((driver / "LINK.json").read_text()) != manifest["software_vulkan"]:
        raise SystemExit("Mesa link metadata changed during the WebGPU link")
    if manifest["software_vulkan"].get("runtime", {"kind": "stock-sdk"}) != runtime:
        raise SystemExit("Mesa runtime identity changed during the WebGPU build")
    verify_archives([arg for arg in manifest["software_vulkan"]["link_args"] if not arg.startswith("-")],
                    manifest["software_vulkan"].get("link_archive_sha256"),
                    required=runtime["kind"] != "stock-sdk")
    notices = driver / "licenses"
    if not notices.is_dir() or not any(notices.rglob("*")):
        raise SystemExit("Missing software Vulkan dependency license notices")
    shutil.copytree(notices, out / "licenses/software-vulkan", dirs_exist_ok=True)
(out / "BUILD.json").write_text(json.dumps(manifest, indent=2) + "\n")
PY
(cd -- "$webgpu_out" && sha256sum webgpu_compute ape-x86_64.elf BUILD.json PREPARED.json \
  cargo-artifacts.jsonl cargo-invocation.json build-commands.json > SHA256SUMS)
printf '\nBuilt %s\n' "$webgpu_out/webgpu_compute"
printf 'Run bash third_party/wgpu_native/test.sh for startup and compute checks.\n'
