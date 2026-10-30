#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
: "${COSMOCC:?Set COSMOCC to a Cosmopolitan compiler or set APELINK}"
: "${TOOLCHAIN_OBJECTS:?Set TOOLCHAIN_OBJECTS to x86-64 object paths}"
: "${TOOLCHAIN_ARM_OBJECTS:?Set TOOLCHAIN_ARM_OBJECTS to ARM64 object paths}"
: "${TOOLCHAIN_DEPS:?Set TOOLCHAIN_DEPS to x86-64 archive paths}"

TOOLCHAIN_OUTPUT=${1:?Usage: link-unified-ape.sh output.com}
COSMO_MAKE="${COSMO_MAKE:-make}"
APELINK="${APELINK:-$(dirname "$(command -v "$COSMOCC")")/apelink}"
test -x "$APELINK" || { echo "Cosmopolitan apelink missing: $APELINK" >&2; exit 1; }

ARM_LUA_DEPS=$("$COSMO_MAKE" -s -f Makefile -f toolchain/lua-link.mk \
  toolchain-lua-link MODE=aarch64)
x86_64_elf="$TOOLCHAIN_OUTPUT.x86_64.elf"
aarch64_elf="$TOOLCHAIN_OUTPUT.aarch64.elf"

"$COSMO_MAKE" -s -j1 -f Makefile -f toolchain/unified-link.mk \
  MODE= TOOLCHAIN_OUTPUT="$x86_64_elf" \
  TOOLCHAIN_OBJECTS="$TOOLCHAIN_OBJECTS" TOOLCHAIN_DEPS="$TOOLCHAIN_DEPS" \
  "$x86_64_elf"
"$COSMO_MAKE" -s -j1 -f Makefile -f toolchain/unified-link.mk \
  MODE=aarch64 TOOLCHAIN_OUTPUT="$aarch64_elf" \
  TOOLCHAIN_OBJECTS="$TOOLCHAIN_ARM_OBJECTS" TOOLCHAIN_DEPS="$ARM_LUA_DEPS" \
  "$aarch64_elf"

"$APELINK" -o "$TOOLCHAIN_OUTPUT" "$x86_64_elf" "$aarch64_elf"
