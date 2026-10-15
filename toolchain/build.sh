#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
CC="${COSMOCC:-}"
if [ -z "$CC" ]; then
  if command -v cosmocc >/dev/null 2>&1; then CC=cosmocc
  elif [ -x .cosmocc/current/bin/cosmocc ]; then CC=.cosmocc/current/bin/cosmocc
  else
    echo "Cosmopolitan compiler missing: set COSMOCC to a cosmocc executable" >&2
    exit 1
  fi
fi
mkdir -p o/toolchain
"$CC" -O2 -o o/toolchain/toolchain.com toolchain/ape_toolchain.c
o/toolchain/toolchain.com selftest
