#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
webgpu_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
webgpu_out=$(cd -- "$webgpu_dir/../.." && pwd)/o/webgpu
if (($# > 1)) || [[ ${1:-} != '' && ${1:-} != --version && ${1:-} != --software ]]; then
  echo 'Usage: bash third_party/wgpu_native/test.sh [--version | --software]' >&2
  exit 2
fi
webgpu_args=()
if [[ ${1:-} == --software ]]; then
  webgpu_args=(--software)
fi
cd -- "$webgpu_out"
sha256sum --check --strict SHA256SUMS
# Use the explicit loader on Linux so execution always uses the packaged APE.
timeout 60s ./ape-x86_64.elf ./webgpu_compute --version
if [[ ${1:-} != --version ]]; then
  timeout 180s ./ape-x86_64.elf ./webgpu_compute "${webgpu_args[@]}"
  timeout 180s ./ape-x86_64.elf ./webgpu_compute "${webgpu_args[@]}" --matmul
  timeout 180s ./ape-x86_64.elf ./webgpu_compute "${webgpu_args[@]}" --raster
fi
sha256sum --check --strict SHA256SUMS
