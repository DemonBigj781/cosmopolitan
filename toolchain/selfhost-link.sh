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
"$COSMOCC" -O2 -std=c11 -pthread -Dmain=machin_entry -c o/toolchain/compiler.c -o o/toolchain/compiler.o
"$COSMOCC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/dispatcher.o
# shellcheck disable=SC2086
TOOLCHAIN_OBJECTS="o/toolchain/dispatcher.o o/toolchain/compiler.o o//toolchain/lua_entry.o" \
TOOLCHAIN_ARM_OBJECTS="o/toolchain/.aarch64/dispatcher.o o/toolchain/.aarch64/compiler.o o/toolchain/.aarch64/lua_entry.o" \
TOOLCHAIN_DEPS="$LUA_DEPS" COSMOCC="$COSMOCC" COSMO_MAKE="$COSMO_MAKE" \
  sh toolchain/link-unified-ape.sh o/toolchain/toolchain.com
o/toolchain/toolchain.com selftest
test "$(o/toolchain/toolchain.com lua -e 'print(6 * 7)')" = 42
o/toolchain/toolchain.com machin-compiler --full toolchain/machin_demo.mfl > o/toolchain/machin_output.c
test -s o/toolchain/machin_output.c
grep -q 'main' o/toolchain/machin_output.c
echo "PASS: self-hosted Machin compiler embedded in single APE"
