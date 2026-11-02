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
webgpu_software_args=()

if (($#)); then
  case "$1" in
    --help|-h)
      cat <<'EOF'
Usage: bash third_party/wgpu_native/build.sh [--software]

Build on Linux x86-64. Required host tools: a C compiler, libclang (for
bindgen), Python 3.12+, curl, git, patch, tar, unzip, sha256sum.
The pinned Rust/Cosmocc SDK and build outputs live under o/.

Output: o/webgpu/webgpu_compute (one Windows/Linux x86-64 APE)
        o/webgpu/ape-x86_64.elf (optional explicit Linux APE loader)

--software also builds Mesa lavapipe and LLVM as Cosmopolitan static libraries.
This mode additionally needs a native C++ compiler, CMake 3.24+, Ninja, and
Python venv support. The resulting executable runs --software without a host
Vulkan loader or driver. Its Mesa outputs use the default o/lavapipe directory.

Run --version to check C/Rust linkage without Vulkan. Run without arguments
for shader dispatch/readback using a host Vulkan loader and driver. Use
--software --matmul to test the embedded CPU driver with f32 matrix operations.
Use --software --raster to draw and read back an offscreen RGBA8 triangle.
This is an experimental headless compute/raster port, not a window system.
EOF
      exit 0 ;;
    --software) webgpu_software=1 ;;
    *) echo 'Unknown argument; see --help.' >&2; exit 2 ;;
  esac
  if (($# != 1)); then
    echo 'Too many arguments; see --help.' >&2
    exit 2
  fi
fi
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
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2}
export WGPU_NATIVE_VERSION=v29.0.1.1

bash "$webgpu_dir/tests/run_abi.sh"
bash "$webgpu_dir/tests/run_loader.sh"
bash "$webgpu_rust" setup
if ((webgpu_software)); then
  bash "$webgpu_repo/third_party/lavapipe/build.sh"
  python3 - "$webgpu_repo/o/lavapipe/LINK.json" > "$webgpu_out/software-link-args" <<'PY'
import json
from pathlib import Path
import sys

manifest = json.loads(Path(sys.argv[1]).read_text())
args = manifest["link_args"]
if not isinstance(args, list) or not args or any(
        not isinstance(arg, str) or not arg or "\0" in arg for arg in args):
    raise SystemExit("Invalid static lavapipe link arguments")
for arg in args:
    if arg.endswith((".so", ".dll", ".dylib")) or ".so." in arg:
        raise SystemExit(f"A software Vulkan link input is a shared library: {arg}")
    sys.stdout.buffer.write(arg.encode() + b"\0")
PY
  mapfile -d '' -t webgpu_software_args < "$webgpu_out/software-link-args"
  webgpu_software_args=(-DCOSMO_WGPU_EMBEDDED_LAVAPIPE=1 "${webgpu_software_args[@]}")
fi
python3 "$webgpu_dir/prepare.py"
# Bindgen must read the same C scalar definitions as the Cosmopolitan compiler.
# Its default Linux target would otherwise discover the build host's headers.
export BINDGEN_EXTRA_CLANG_ARGS="-I\"$webgpu_sdk/vendor/cosmocc/include\" -include \"$webgpu_sdk/vendor/cosmocc/include/libc/normalize.inc\" ${BINDGEN_EXTRA_CLANG_ARGS:-}"
bash "$webgpu_rust" cargo --config "$webgpu_out/cargo.cosmo.toml" \
  build --manifest-path "$webgpu_out/source/Cargo.toml" --locked \
  --release --lib --no-default-features --features wgsl,vulkan \
  --message-format=json-render-diagnostics > "$webgpu_out/cargo-artifacts.jsonl"

webgpu_archive="$webgpu_repo/o/rust-ape/build/x86_64-unknown-linux-musl/release/libwgpu_native.a"
test -s "$webgpu_archive"
# PTHREAD_MUTEX_INITIALIZER in this Cosmocc release initializes trailing
# private fields implicitly; -Wextra otherwise warns about the SDK macro.
"$webgpu_sdk/generated/linker-x86_64.bash" \
  -mcosmo -std=c11 -O2 -g -fno-omit-frame-pointer -fno-stack-protector \
  -Wall -Wextra -Werror -Wno-missing-field-initializers \
  -I"$webgpu_dir/upstream/ffi" \
  -I"$webgpu_dir/upstream/ffi/webgpu-headers" \
  -I"$webgpu_dir/runtime" -I"$webgpu_out/generated" \
  "$webgpu_dir/examples/compute.c" \
  "$webgpu_dir/runtime/vulkan_loader.c" \
  "$webgpu_dir/runtime/win64_bridge.c" \
  "$webgpu_archive" "${webgpu_software_args[@]}" \
  -Wl,--gc-sections -pthread -lm -ldl \
  -o "$webgpu_out/webgpu_compute.dbg"

# Keep APE/PE support. The Linux-only apelink -V1 option is not used.
sh "$webgpu_sdk/vendor/cosmocc/bin/apelink" \
  -l "$webgpu_sdk/vendor/cosmocc/bin/ape-x86_64.elf" \
  -o "$webgpu_out/webgpu_compute" "$webgpu_out/webgpu_compute.dbg"
python3 "$webgpu_dir/configure_pe.py" "$webgpu_out/webgpu_compute"
cp -- "$webgpu_sdk/vendor/cosmocc/bin/ape-x86_64.elf" "$webgpu_out/ape-x86_64.elf"
chmod +x -- "$webgpu_out/webgpu_compute" "$webgpu_out/ape-x86_64.elf"

python3 "$webgpu_dir/package_notices.py"
python3 - "$webgpu_out" "$webgpu_dir/Cargo.cosmo.lock" "$webgpu_software" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shutil

out, lock = map(Path, sys.argv[1:3])
software = sys.argv[3] == "1"
manifest = {
    "target": "x86_64 Cosmopolitan APE",
    "wgpu_native": "v29.0.1.1",
    "wgpu_revision": "6aed50955d934ac36049ba8d002034841633ae02",
    "rust_ape_revision": "0e2d0a89f4c089c7f21377a9c7ce51a6e82c2791",
    "cosmocc": "4.0.2",
    "rust": "nightly-2026-07-28",
    "features": ["wgsl", "vulkan"],
    "verified_workloads": ["collatz", "f32-matmul", "rgba8-triangle-raster"],
    "embedded_software_vulkan": software,
    "windows_native_stack_reserve": 8 * 1024 * 1024,
    "windows_native_stack_commit": 4096,
    "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    "application_sha256": hashlib.sha256((out / "webgpu_compute").read_bytes()).hexdigest(),
    "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "source_dirty": bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], text=True)),
}
if software:
    driver = out.parent / "lavapipe"
    manifest["software_vulkan"] = json.loads((driver / "LINK.json").read_text())
    notices = driver / "licenses"
    if not notices.is_dir() or not any(notices.rglob("*")):
        raise SystemExit("Missing software Vulkan dependency license notices")
    shutil.copytree(notices, out / "licenses/software-vulkan", dirs_exist_ok=True)
(out / "BUILD.json").write_text(json.dumps(manifest, indent=2) + "\n")
PY
(cd -- "$webgpu_out" && sha256sum webgpu_compute ape-x86_64.elf BUILD.json > SHA256SUMS)
printf '\nBuilt %s\n' "$webgpu_out/webgpu_compute"
printf 'Run bash third_party/wgpu_native/test.sh for startup and compute checks.\n'
