#!/bin/sh
# Compile the actual self-hosted Machin compiler into one Cosmopolitan APE.
set -eu
cd "$(dirname "$0")/.."
: "${MACHIN:?Set MACHIN to a host Machin bootstrap compiler}"
: "${COSMOCC:?Set COSMOCC to the Cosmopolitan C compiler}"
: "${MACHIN_SRC:?Set MACHIN_SRC to a checkout of javimosch/machin}"
mkdir -p o/toolchain
srcs="selfhost/lex.src selfhost/parse.src selfhost/check.src selfhost/checkgen.src selfhost/cgen.src selfhost/cgbuiltin.src selfhost/alias.src selfhost/cgagg.src selfhost/cgffi.src selfhost/cgprelude.src selfhost/cgprog.src selfhost/compile.src selfhost/cgmain.src"
# shellcheck disable=SC2086
(cd "$MACHIN_SRC" && "$MACHIN" encode $srcs) > o/toolchain/compiler.mfl
test -s o/toolchain/compiler.mfl
"$MACHIN" build o/toolchain/compiler.mfl --emit-c > o/toolchain/compiler.c
test -s o/toolchain/compiler.c
COSMO_MAKE="${COSMO_MAKE:-make}"
LUA_DEPS=$(COSMO_MAKE="$COSMO_MAKE" sh toolchain/prepare-lua.sh)
"$COSMO_MAKE" -s -j1 -f Makefile -f toolchain/package-objects.mk MODE= \
  o//toolchain/ape_toolchain.o o//toolchain/compiler.o
"$COSMO_MAKE" -s -j1 -f Makefile -f toolchain/package-objects.mk MODE=aarch64 \
  o/aarch64/toolchain/ape_toolchain.o o/aarch64/toolchain/compiler.o
mkdir -p o/toolchain/.aarch64
for object in o/aarch64/toolchain/ape_toolchain.o o/aarch64/toolchain/compiler.o; do
  cp "$object" "o/toolchain/.aarch64/${object##*/}"
done
# shellcheck disable=SC2086
TOOLCHAIN_OBJECTS="o//toolchain/ape_toolchain.o o//toolchain/compiler.o o//toolchain/lua_entry.o" \
TOOLCHAIN_ARM_OBJECTS="o/toolchain/.aarch64/ape_toolchain.o o/toolchain/.aarch64/compiler.o o/toolchain/.aarch64/lua_entry.o" \
TOOLCHAIN_DEPS="$LUA_DEPS" COSMOCC="$COSMOCC" COSMO_MAKE="$COSMO_MAKE" \
  sh toolchain/link-unified-ape.sh o/toolchain/toolchain.com
o/toolchain/toolchain.com selftest
test "$(o/toolchain/toolchain.com lua -e 'print(6 * 7)')" = 42
o/toolchain/toolchain.com machin-compiler --full toolchain/machin_demo.mfl > o/toolchain/machin_output.c
test -s o/toolchain/machin_output.c
grep -q 'main' o/toolchain/machin_output.c
echo "PASS: self-hosted Machin compiler embedded in single APE"
