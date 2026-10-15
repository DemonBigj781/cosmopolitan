#!/bin/sh
# Integration test: the embedded Machin compiler produces C which Cosmopolitan
# turns into a SECOND APE. The C compiler remains external at this stage.
set -eu
cd "$(dirname "$0")/.."
: "${COSMOCC:?Set COSMOCC to the Cosmopolitan compiler}"
test -f o/toolchain/toolchain.com
mkdir -p o/toolchain/roundtrip
./o/toolchain/toolchain.com machin-compiler --full toolchain/machin_demo.mfl > o/toolchain/roundtrip/generated.c
test -s o/toolchain/roundtrip/generated.c
"$COSMOCC" -O2 -std=c11 -pthread -o o/toolchain/roundtrip/generated.com o/toolchain/roundtrip/generated.c
o/toolchain/roundtrip/generated.com > o/toolchain/roundtrip/output.txt
grep -Fx 'Machin linked into Cosmopolitan APE' o/toolchain/roundtrip/output.txt
echo 'PASS: embedded Machin compiler generated C, external Cosmopolitan compiler produced executable APE'
