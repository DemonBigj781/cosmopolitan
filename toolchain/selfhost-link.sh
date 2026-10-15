#!/bin/sh
# Compile the actual self-hosted Machin compiler into one Cosmopolitan APE.
set -eu
cd "$(dirname "$0")/.."
: "${MACHIN:?Set MACHIN to a host Machin bootstrap compiler}"
: "${COSMOCC:?Set COSMOCC to the Cosmopolitan C compiler}"
: "${MACHIN_SRC:?Set MACHIN_SRC to a checkout of javimosch/machin}"
mkdir -p o/toolchain
srcs="selfhost/lex.src selfhost/parse.src selfhost/check.src selfhost/checkgen.src selfhost/cgen.src selfhost/cgbuiltin.src selfhost/alias.src selfhost/cgagg.src selfhost/cgffi.src selfhost/cgprelude.src selfhost/cgprog.src selfhost/compile.src selfhost/cgmain.src"
(cd "$MACHIN_SRC" && "$MACHIN" encode $srcs) > o/toolchain/compiler.mfl
test -s o/toolchain/compiler.mfl
"$MACHIN" build o/toolchain/compiler.mfl --emit-c > o/toolchain/compiler.c
test -s o/toolchain/compiler.c
"$COSMOCC" -O2 -std=c11 -pthread -Dmain=machin_entry -c o/toolchain/compiler.c -o o/toolchain/compiler.o
"$COSMOCC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/dispatcher.o
"$COSMOCC" -o o/toolchain/toolchain.com o/toolchain/dispatcher.o o/toolchain/compiler.o
o/toolchain/toolchain.com selftest
o/toolchain/toolchain.com machin-compiler --full toolchain/machin_demo.mfl > o/toolchain/machin_output.c
test -s o/toolchain/machin_output.c
grep -q 'main' o/toolchain/machin_output.c
echo "PASS: self-hosted Machin compiler embedded in single APE"
