#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
: "${MACHIN:?Set MACHIN to machin binary}"
: "${COSMOCC:?Set COSMOCC to cosmocc binary}"
mkdir -p o/toolchain
"$MACHIN" build toolchain/machin_demo.mfl --emit-c > o/toolchain/machin_demo.c
test -s o/toolchain/machin_demo.c
COSMO_MAKE="${COSMO_MAKE:-make}"
LUA_DEPS=$(COSMO_MAKE="$COSMO_MAKE" sh toolchain/prepare-lua.sh)
"$COSMOCC" -O2 -Dmain=machin_entry -c o/toolchain/machin_demo.c -o o/toolchain/machin_demo.o
"$COSMOCC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/ape_toolchain.o
# shellcheck disable=SC2086
TOOLCHAIN_OBJECTS="o/toolchain/ape_toolchain.o o/toolchain/machin_demo.o o//toolchain/lua_entry.o" \
TOOLCHAIN_ARM_OBJECTS="o/toolchain/.aarch64/ape_toolchain.o o/toolchain/.aarch64/machin_demo.o o/toolchain/.aarch64/lua_entry.o" \
TOOLCHAIN_DEPS="$LUA_DEPS" COSMOCC="$COSMOCC" COSMO_MAKE="$COSMO_MAKE" \
  sh toolchain/link-unified-ape.sh o/toolchain/toolchain.com
o/toolchain/toolchain.com selftest
o/toolchain/toolchain.com machin-demo
test "$(o/toolchain/toolchain.com lua -e 'print(6 * 7)')" = 42
