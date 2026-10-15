#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
: "${MACHIN:?Set MACHIN to machin binary}"
: "${COSMOCC:?Set COSMOCC to cosmocc binary}"
mkdir -p o/toolchain
"$MACHIN" build toolchain/machin_demo.mfl --emit-c > o/toolchain/machin_demo.c
test -s o/toolchain/machin_demo.c
"$COSMOCC" -O2 -Dmain=machin_entry -c o/toolchain/machin_demo.c -o o/toolchain/machin_demo.o
"$COSMOCC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/ape_toolchain.o
"$COSMOCC" -o o/toolchain/toolchain.com o/toolchain/ape_toolchain.o o/toolchain/machin_demo.o
o/toolchain/toolchain.com selftest
o/toolchain/toolchain.com machin-demo
