#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
COSMO_MAKE="${COSMO_MAKE:-make}"

LUA_DEPS=$("$COSMO_MAKE" -s -f Makefile -f toolchain/lua-link.mk \
  toolchain-lua-link MODE=)
ARM_LUA_DEPS=$("$COSMO_MAKE" -s -f Makefile -f toolchain/lua-link.mk \
  toolchain-lua-link MODE=aarch64)

ensure_archive_members() {
  archive=$1
  members=$(ar t "$archive" 2>/dev/null || true)
  if [ -z "$members" ]; then
    echo "Rebuilding empty archive: $archive" >&2
    case "$archive" in
      o/aarch64/*) "$COSMO_MAKE" -s -B -j1 MODE=aarch64 "$archive" ;;
      o/*) "$COSMO_MAKE" -s -B -j1 MODE= "$archive" ;;
      *) echo "Unexpected archive path: $archive" >&2; exit 1 ;;
    esac
  fi
}

# Build both architecture closures and the wrapper with Cosmopolitan's normal
# object rules. MODE must match each target tree so make applies the correct
# compiler, flags, dependencies, and archive recipes.
# shellcheck disable=SC2086
"$COSMO_MAKE" -s -j1 MODE= $LUA_DEPS o//toolchain/lua_entry.o
# shellcheck disable=SC2086
"$COSMO_MAKE" -s -j1 MODE=aarch64 $ARM_LUA_DEPS o/aarch64/toolchain/lua_entry.o

# A stale empty archive can be considered up to date by make after an
# interrupted or partial cross-architecture build. It omits startup objects
# such as cosmo2.o and then causes unresolved runtime symbols at link time.
ensure_archive_members o//libc/runtime/runtime.a
ensure_archive_members o/aarch64/libc/runtime/runtime.a

# cosmocc's multi-architecture linker looks for ARM64 companions in each
# archive's .aarch64 directory. Copy the exact Lua closure, not only zlib.
# shellcheck disable=SC2086
for archive in $LUA_DEPS; do
  relative=${archive#o/}
  relative=${relative#/}
  arm_archive="o/aarch64/$relative"
  companion="o/$relative"
  companion="$(dirname "$companion")/.aarch64/$(basename "$companion")"
  test -f "$arm_archive" || { echo "ARM64 archive missing: $arm_archive" >&2; exit 1; }
  mkdir -p "$(dirname "$companion")"
  cp "$arm_archive" "$companion"
done

mkdir -p o/toolchain/.aarch64
cp o/aarch64/toolchain/lua_entry.o o/toolchain/.aarch64/lua_entry.o
test -s o/toolchain/lua_entry.o
test -s o/toolchain/.aarch64/lua_entry.o
printf '%s\n' "$LUA_DEPS"
