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

usage() {
  cat <<'EOF'
Usage: third_party/rust_ape/run.sh COMMAND [ARGUMENTS]

  setup                  Install the pinned SDK under o/rust-ape.
  gen-shim               Regenerate ABI tables from the installed runtime headers.
  build PROJECT [ARGS]   Build an APE with upstream xtask (both CPU targets).
  generate PROJECT      Generate a Rust project using the patched SDK.
  cargo [CARGO ARGS]     Run Cargo for the Cosmopolitan target and patched std.
  paths                  Print SDK paths without installing anything.

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
python3 "$rust_ape_source/stage.py" "$rust_ape_source/upstream" "$rust_ape_sdk"
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

if [[ "$rust_ape_command" == setup ]]; then
  (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- setup "$@")
  python3 "$rust_ape_source/prepare.py" "$rust_ape_sdk"
  exit 0
elif [[ ! -f "$rust_ape_sdk/generated/x86_64-unknown-linux-musl.json" ]]; then
  (cd -- "$rust_ape_sdk" && cargo run --locked --package xtask -- setup)
fi
python3 "$rust_ape_source/prepare.py" "$rust_ape_sdk"

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
exec cargo --config "$rust_ape_sdk/generated/cargo.cosmo.toml" \
  -Z json-target-spec -Z build-std=std,panic_abort,panic_unwind \
  -Z build-std-features= "$@"
