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
COSMO_MAKE="${COSMO_MAKE:-make}"
LUA_DEPS=$(COSMO_MAKE="$COSMO_MAKE" sh toolchain/prepare-lua.sh)
"$CC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/ape_toolchain.o
# shellcheck disable=SC2086
TOOLCHAIN_OBJECTS="o/toolchain/ape_toolchain.o o//toolchain/lua_entry.o" \
TOOLCHAIN_ARM_OBJECTS="o/toolchain/.aarch64/ape_toolchain.o o/toolchain/.aarch64/lua_entry.o" \
TOOLCHAIN_DEPS="$LUA_DEPS" COSMOCC="$CC" COSMO_MAKE="$COSMO_MAKE" \
  sh toolchain/link-unified-ape.sh o/toolchain/toolchain.com
o/toolchain/toolchain.com selftest
test "$(o/toolchain/toolchain.com lua -e 'print(6 * 7)')" = 42
