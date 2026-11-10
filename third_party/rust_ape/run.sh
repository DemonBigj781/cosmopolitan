#!/usr/bin/env bash
# Build Rust against Cosmopolitan's runtime using a pinned, patched Rust SDK.
set -euo pipefail

rust_ape_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
rust_ape_repo=$(cd -- "$rust_ape_source/../.." && pwd)
rust_ape_build=${COSMO_RUST_BUILD_ROOT:-"$rust_ape_repo/o/rust-ape"}
mkdir -p -- "$rust_ape_build"
rust_ape_build=$(cd -- "$rust_ape_build" && pwd)
rust_ape_sdk="$rust_ape_build/sdk"
rust_ape_nightly=nightly-2026-07-28
rust_ape_profile=${COSMO_RUNTIME_PROFILE:-stock-sdk}
case "$rust_ape_profile" in
  stock-sdk|source-built) ;;
  *) echo 'COSMO_RUNTIME_PROFILE must be stock-sdk or source-built.' >&2; exit 2 ;;
esac
if [[ "$rust_ape_profile" == source-built ]]; then
  : "${COSMO_RUNTIME_SDK:?Source-built profile requires COSMO_RUNTIME_SDK from toolchain/source_runtime.py}"
elif [[ -n ${COSMO_RUNTIME_SDK:-} ]]; then
  echo 'COSMO_RUNTIME_SDK requires COSMO_RUNTIME_PROFILE=source-built.' >&2
  exit 2
fi

usage() {
  cat <<'EOF'
Usage: third_party/rust_ape/run.sh COMMAND [ARGUMENTS]

  setup                  Install the selected SDK profile under o/rust-ape.
  gen-shim               Regenerate ABI constants using the selected runtime.
  build PROJECT [ARGS]   Build an APE with upstream xtask (both CPU targets).
  generate PROJECT      Generate a Rust project using the patched SDK.
  cargo [CARGO ARGS]     Run Cargo for the Cosmopolitan target and patched std.
  paths                  Print SDK paths without installing anything.

COSMO_RUNTIME_PROFILE=source-built requires COSMO_RUNTIME_SDK to identify a
complete source-built C/C++ SDK. Stock and source profiles require separate build
roots; runtime identity and ABI tables are checked before compiling Rust.

The build host must be Linux x86-64. COSMO_RUST_BUILD_ROOT changes the build
directory. COSMO_RUST_ARCH selects x86_64 (default) or aarch64 for `cargo`.
The `cargo` command uses the caller's working directory. Its outputs are in
o/rust-ape/build. It is intended for static libraries and explicit Rust bins.
EOF
}

rust_ape_command=${1:-help}
if (($#)); then shift; fi
case "$rust_ape_command" in
  help|-h|--help) usage; exit 0 ;;
  paths)
    printf 'SDK=%s\nCARGO_HOME=%s\nRUSTUP_HOME=%s\n' "$rust_ape_sdk" \
      "$rust_ape_build/toolchain/cargo" "$rust_ape_build/toolchain/rustup"
    exit 0 ;;
  setup|build|generate|gen-shim|cargo) ;;
  *) usage >&2; exit 2 ;;
esac

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'The pinned Rust SDK bootstrap currently requires a Linux x86-64 build host.' >&2
  exit 1
fi
for rust_ape_tool in curl sha256sum tar unzip patch python3 cc; do
  if ! command -v "$rust_ape_tool" >/dev/null; then
    printf 'Missing build prerequisite: %s\n' "$rust_ape_tool" >&2
    exit 1
  fi
done

# Keep rustup, Cargo packages, compiler downloads and source materialization
# outside the tracked snapshot. Never change the user's default Rust toolchain.
export CARGO_HOME="$rust_ape_build/toolchain/cargo"
export RUSTUP_HOME="$rust_ape_build/toolchain/rustup"
export PATH="$CARGO_HOME/bin:$PATH"
export RUSTUP_TOOLCHAIN="$rust_ape_nightly"
mkdir -p -- "$rust_ape_build/toolchain/downloads" "$rust_ape_sdk"
python3 "$rust_ape_source/stage.py" "$rust_ape_source/upstream" "$rust_ape_sdk" \
  --runtime-profile "$rust_ape_profile"
if [[ "$rust_ape_profile" == source-built ]]; then
  for argument in "$@"; do
    if [[ "$rust_ape_command" == setup && "$argument" == --force ]]; then
      echo 'Source-runtime setup cannot reset a bound SDK; use a new build root.' >&2
      exit 2
    fi
  done
  if [[ -n ${COSMO:-} && "$COSMO" != "$rust_ape_sdk/vendor/cosmocc" ]]; then
    echo 'COSMO override conflicts with the selected source-runtime SDK.' >&2
    exit 2
  fi
  if [[ "$rust_ape_command" != setup && "$rust_ape_command" != gen-shim &&
        -f "$rust_ape_sdk/.cosmo-runtime-tables-ready" ]]; then
    python3 "$rust_ape_source/runtime_profile.py" verify --sdk "$rust_ape_sdk" >/dev/null
  fi
fi
export COSMO="$rust_ape_sdk/vendor/cosmocc"

if [[ ! -x "$CARGO_HOME/bin/rustup" ]]; then
  rust_ape_init="$rust_ape_build/toolchain/downloads/rustup-init"
  rust_ape_init_sha=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
  if [[ ! -f "$rust_ape_init" ]]; then
    curl -fLsS --retry 3 --connect-timeout 20 \
      https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-unknown-linux-gnu/rustup-init \
      -o "$rust_ape_init.part"
    mv -- "$rust_ape_init.part" "$rust_ape_init"
  fi
  printf '%s  %s\n' "$rust_ape_init_sha" "$rust_ape_init" | sha256sum --check --status
  chmod +x -- "$rust_ape_init"
  "$rust_ape_init" -y --no-modify-path --profile minimal \
    --default-host x86_64-unknown-linux-gnu --default-toolchain none
fi
rustup set auto-self-update disable >/dev/null

if ! rustup run "$rust_ape_nightly" rustc --version >/dev/null 2>&1; then
  rustup toolchain install "$rust_ape_nightly" --profile minimal --component rust-src
fi

if [[ "$rust_ape_profile" == source-built &&
      -f "$rust_ape_sdk/.cosmo-runtime-binding.json" ]]; then
  # Upstream setup owns a stock SDK fetch and may delete vendor/cosmocc when
  # its download stamp is absent. It must never run over a bound source SDK.
  if [[ "$rust_ape_command" == setup && $# != 0 ]]; then
    echo 'Bound source-runtime setup takes no arguments; use a fresh root to reset.' >&2
    exit 2
  fi
  for rust_ape_target in x86_64 aarch64; do
    for rust_ape_generated in "linker-$rust_ape_target.bash" "ar-$rust_ape_target.bash" \
                              "$rust_ape_target-unknown-linux-musl.json"; do
      if [[ ! -f "$rust_ape_sdk/generated/$rust_ape_generated" ]]; then
        echo 'Bound source-runtime SDK is incomplete; choose a new COSMO_RUST_BUILD_ROOT.' >&2
        exit 1
      fi
    done
  done
elif [[ "$rust_ape_command" == setup ]]; then
  (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- setup "$@")
elif [[ ! -f "$rust_ape_sdk/generated/x86_64-unknown-linux-musl.json" ]]; then
  (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- setup)
fi
if [[ "$rust_ape_profile" == source-built ]]; then
  python3 "$rust_ape_source/runtime_profile.py" activate --sdk "$rust_ape_sdk" \
    --runtime "$COSMO_RUNTIME_SDK" >/dev/null
  # This generator preprocesses the selected source headers. It does not execute
  # the target program; downstream compile-time assertions verify the constants.
  if [[ "$rust_ape_command" != gen-shim && ( "$rust_ape_command" == setup ||
        ! -f "$rust_ape_sdk/.cosmo-runtime-tables-ready" ) ]]; then
    rm -f -- "$rust_ape_sdk/.cosmo-runtime-tables-ready"
    (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- gen-shim)
    python3 "$rust_ape_source/runtime_profile.py" record-tables --sdk "$rust_ape_sdk"
    touch "$rust_ape_sdk/.cosmo-runtime-tables-ready"
  fi
  if [[ "$rust_ape_command" != gen-shim ]]; then
    python3 "$rust_ape_source/runtime_profile.py" verify --sdk "$rust_ape_sdk" >/dev/null
  fi
else
  python3 "$rust_ape_source/prepare.py" "$rust_ape_sdk"
fi
if [[ "$rust_ape_command" == setup ]]; then
  exit 0
fi
if [[ "$rust_ape_command" == gen-shim ]]; then
  if [[ "$rust_ape_profile" == source-built && " $* " != *" --dry-run "* ]]; then
    rm -f -- "$rust_ape_sdk/.cosmo-runtime-tables-ready"
  fi
  (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- gen-shim "$@")
  if [[ "$rust_ape_profile" == source-built && " $* " != *" --dry-run "* ]]; then
    python3 "$rust_ape_source/runtime_profile.py" record-tables --sdk "$rust_ape_sdk"
    touch "$rust_ape_sdk/.cosmo-runtime-tables-ready"
    python3 "$rust_ape_source/runtime_profile.py" verify --sdk "$rust_ape_sdk" >/dev/null
  fi
  exit 0
fi

if [[ "$rust_ape_command" != cargo ]]; then
  if (($#)); then
    rust_ape_project=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$1")
    shift
    set -- "$rust_ape_project" "$@"
  fi
  (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- "$rust_ape_command" "$@")
  exit 0
fi

rust_ape_arch=${COSMO_RUST_ARCH:-x86_64}
case "$rust_ape_arch" in
  x86_64|aarch64) ;;
  *) echo 'COSMO_RUST_ARCH must be x86_64 or aarch64.' >&2; exit 2 ;;
esac
rust_ape_triple="$rust_ape_arch-unknown-linux-musl"
rust_ape_target_var=${rust_ape_triple//-/_}
export CARGO_BUILD_TARGET="$rust_ape_sdk/generated/$rust_ape_triple.json"
export CARGO_TARGET_DIR="$rust_ape_build/build"
export __CARGO_TESTS_ONLY_SRC_ROOT="$rust_ape_sdk/vendor/library"
# An encoded flag variable takes precedence over RUSTFLAGS and could silently
# discard the runtime cfgs below. Additional caller flags belong in RUSTFLAGS.
unset CARGO_ENCODED_RUSTFLAGS
export RUSTFLAGS="${RUSTFLAGS:+$RUSTFLAGS }--cfg rustix_use_libc --cfg polling_test_poll_backend --cfg mio_unsupported_force_waker_pipe --cfg rust_ape_shim"
export "CC_$rust_ape_target_var=$rust_ape_sdk/vendor/cosmocc/bin/$rust_ape_arch-unknown-cosmo-cc"
export "CXX_$rust_ape_target_var=$rust_ape_sdk/vendor/cosmocc/bin/$rust_ape_arch-unknown-cosmo-c++"
export "AR_$rust_ape_target_var=$rust_ape_sdk/generated/ar-$rust_ape_arch.bash"
rust_ape_cflags="-fno-stack-protector -mstack-protector-guard=global -D_GNU_SOURCE -DSOCK_CLOEXEC=SOCK_CLOEXEC -DMADV_FREE=MADV_FREE -DHAVE_ENDIAN_H -include $rust_ape_sdk/scripts/cdeps-predef.h"
export "CFLAGS_$rust_ape_target_var=$rust_ape_cflags"
export "CXXFLAGS_$rust_ape_target_var=$rust_ape_cflags"

python3 - "$rust_ape_sdk" <<'PY'
import json
import pathlib
import sys

sdk = pathlib.Path(sys.argv[1])
config = "[patch.crates-io]\n"
for name in ("libc", "errno"):
    config += f"{name} = {{ path = {json.dumps(str(sdk / 'vendor/patches' / name))} }}\n"
(sdk / "generated/cargo.cosmo.toml").write_text(config)
PY
rust_ape_cargo_args=(--config "$rust_ape_sdk/generated/cargo.cosmo.toml"
  -Z json-target-spec -Z build-std=std,panic_abort,panic_unwind
  -Z build-std-features= "$@")
if [[ -n ${COSMO_RUST_CARGO_RECEIPT:-} ]]; then
  # This is invocation evidence only. A successful consumer must separately
  # bind its hash to the selected compiler artifact and verified runtime.
  python3 - "$COSMO_RUST_CARGO_RECEIPT" "$rust_ape_sdk" "$rust_ape_repo" \
    "$rust_ape_source/run.sh" "$rust_ape_nightly" "${rust_ape_cargo_args[@]}" <<'PY_RECEIPT'
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

output, sdk, repo, wrapper = map(Path, sys.argv[1:5])
nightly = sys.argv[5]
argv = ["cargo", *sys.argv[6:]]
sys.path.insert(0, str(repo / "toolchain"))
from runtime_identity import selected_runtime

runtime = selected_runtime(sdk / "vendor/cosmocc")
if runtime["kind"] != "stock-sdk":
    sys.path.insert(0, str(repo / "third_party/rust_ape"))
    from runtime_profile import verify_binding
    verify_binding(sdk)
configs = [Path(argv[index + 1]) for index, arg in enumerate(argv[:-1]) if arg == "--config"]
# The wrapper uses file configs, as does WebGPU. Refuse ambiguous inline
# configurations when this optional file-receipt contract is requested.
if any(not path.is_file() for path in configs):
    raise SystemExit("Cargo invocation receipt requires --config file paths")
keys = (
    "RUSTUP_TOOLCHAIN", "CARGO_BUILD_TARGET", "CARGO_TARGET_DIR", "CARGO_BUILD_JOBS",
    "RUSTFLAGS", "__CARGO_TESTS_ONLY_SRC_ROOT", "WGPU_NATIVE_VERSION",
    "BINDGEN_EXTRA_CLANG_ARGS", "LIBCLANG_PATH", "COSMO_RUNTIME_PROFILE", "COSMO",
    "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_INCREMENTAL",
    "CARGO_PROFILE_RELEASE_OPT_LEVEL", "CARGO_PROFILE_RELEASE_DEBUG",
    "CARGO_PROFILE_RELEASE_SPLIT_DEBUGINFO", "CARGO_PROFILE_RELEASE_STRIP",
    "CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS", "CARGO_PROFILE_RELEASE_OVERFLOW_CHECKS",
    "CARGO_PROFILE_RELEASE_LTO", "CARGO_PROFILE_RELEASE_PANIC",
    "CARGO_PROFILE_RELEASE_INCREMENTAL", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
)
triple = "_".join(Path(os.environ["CARGO_BUILD_TARGET"]).stem.split("-"))
keys += tuple(prefix + "_" + triple for prefix in ("CC", "CXX", "AR", "CFLAGS", "CXXFLAGS"))
checksum = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
receipt = {
    "schema_version": 1,
    "status": "invocation-only",
    "argv": argv,
    "cwd": str(Path.cwd()),
    "rust": nightly,
    "runtime": runtime,
    "wrapper_sha256": checksum(wrapper),
    "config_sha256": {str(path.absolute()): checksum(path) for path in configs},
    "target_sha256": checksum(Path(os.environ["CARGO_BUILD_TARGET"])),
    "environment": {key: os.environ[key] for key in keys if key in os.environ},
    "ignored_environment": ["CARGO_ENCODED_RUSTFLAGS"],
}
output.parent.mkdir(parents=True, exist_ok=True)
if output.is_symlink():
    raise SystemExit("Cargo invocation receipt destination cannot be a symlink")
fd, temporary = tempfile.mkstemp(prefix=".cargo-receipt-", dir=output.parent)
try:
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, output)
finally:
    Path(temporary).unlink(missing_ok=True)
PY_RECEIPT
fi
exec cargo "${rust_ape_cargo_args[@]}"
