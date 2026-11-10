#!/bin/sh
# cosmopolitan toolchain packager
#
#     tool/cosmocc/package.sh
#     cd cosmocc
#     zip -ry9 ../cosmocc.zip .
#

set -ex

mode() {
  case $(uname -m) in
    arm64|aarch64)  echo aarch64  ;;
    *)              echo          ;;
  esac
}

_nproc() {
  case $(uname -s) in
    Darwin) sysctl -n hw.logicalcpu ;;
    *)      nproc                   ;;
  esac
}

TMPDIR=${TMPDIR:-/tmp}
OUTDIR=${1:-cosmocc}
APELINK=o/$(mode)/tool/build/apelink
DEDUPE=o/$(mode)/tool/build/dedupe
AMD64=${2:-x86_64}
ARM64=${3:-aarch64}
if [ "${COSMO_PACKAGE_JOBS+x}" = x ]; then
  case "$COSMO_PACKAGE_JOBS" in
    ''|*[!0-9]*|0) echo 'COSMO_PACKAGE_JOBS must be a positive integer' >&2; exit 1 ;;
  esac
  # Reject all-zero strings, without overflowing shell integer arithmetic.
  case "$COSMO_PACKAGE_JOBS" in
    *[1-9]*) NPROC=$COSMO_PACKAGE_JOBS ;;
    *) echo 'COSMO_PACKAGE_JOBS must be a positive integer' >&2; exit 1 ;;
  esac
else
  NPROC=$(($(_nproc)/2))
fi
GCCVER=14.1.0

# One declarative pin source is also consumed by toolchain/source_runtime.py.
# Direct callers need only POSIX shell and the original download/hash tools.
COMPILER_SOURCE_ROOT=$PWD
COMPILER_ARCHIVES=$PWD/tool/cosmocc/compiler-archives.tsv
ARM64_COMPILER_URL=
AMD64_COMPILER_URL=
LLVM_COMPILER_URL=
archive_count=0
while read -r archive checksum url extra || [ -n "$archive$checksum$url$extra" ]; do
  if [ -n "$extra" ] || [ "${#checksum}" -ne 64 ]; then
    echo 'Invalid compiler archive pin row' >&2
    exit 1
  fi
  case "$checksum" in *[!0-9a-f]*) echo 'Invalid compiler archive SHA256' >&2; exit 1 ;; esac
  case "$url" in
    https://github.com/ahgamut/superconfigure/releases/download/*/"$archive") ;;
    *) echo 'Invalid official compiler archive URL' >&2; exit 1 ;;
  esac
  release=${url#https://github.com/ahgamut/superconfigure/releases/download/}
  release=${release%/"$archive"}
  case "$release" in
    ''|*[!A-Za-z0-9_.-]*) echo 'Invalid compiler archive release tag' >&2; exit 1 ;;
  esac
  case "$archive" in
    aarch64-gcc.zip)
      [ -z "$ARM64_COMPILER_URL" ] || exit 1
      ARM64_COMPILER_URL=$url; ARM64_COMPILER_SHA256=$checksum ;;
    x86_64-gcc.zip)
      [ -z "$AMD64_COMPILER_URL" ] || exit 1
      AMD64_COMPILER_URL=$url; AMD64_COMPILER_SHA256=$checksum ;;
    llvm.zip)
      [ -z "$LLVM_COMPILER_URL" ] || exit 1
      LLVM_COMPILER_URL=$url; LLVM_COMPILER_SHA256=$checksum ;;
    *) echo 'Unknown compiler archive' >&2; exit 1 ;;
  esac
  archive_count=$((archive_count + 1))
done < "$COMPILER_ARCHIVES"
[ "$archive_count" -eq 3 ] || { echo 'Missing compiler archive pins' >&2; exit 1; }

setup_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    true
  elif command -v shasum >/dev/null 2>&1; then
    sha256sum() { shasum -a 256 "$@"; }
  elif command -v "$COMPILER_SOURCE_ROOT/o/build/sha256sum" >/dev/null 2>&1; then
    sha256sum() { "$COMPILER_SOURCE_ROOT/o/build/sha256sum" "$@"; }
  else
    echo please install sha256sum >&2
    exit 1
  fi
}

verify_archive() {
  if [ ! -f "$1" ] || [ -L "$1" ]; then
    echo 'Compiler archive must be a regular file' >&2
    exit 1
  fi
  printf '%s\n' "$2  $1" | sha256sum -c - || exit 1
}

fetch() {
  filename=$(basename "$1")
  if [ "${COSMO_PACKAGE_ARCHIVE_CACHE+x}" = x ]; then
    # Cache mode never falls back to the network, even if files disappear.
    verify_archive "$COSMO_PACKAGE_ARCHIVE_CACHE/$filename" "$2"
    cp "$COSMO_PACKAGE_ARCHIVE_CACHE/$filename" "$filename"
  elif command -v wget >/dev/null; then
    wget "$1"
  else
    curl -LO "$1"
  fi
  setup_sha256
  verify_archive "$filename" "$2"
}

# Fail before the first make if any explicitly provided cached input is wrong.
if [ "${COSMO_PACKAGE_ARCHIVE_CACHE+x}" = x ]; then
  case "$COSMO_PACKAGE_ARCHIVE_CACHE" in
    /*) ;;
    *) echo 'Compiler archive cache must be an absolute directory' >&2; exit 1 ;;
  esac
  if [ ! -d "$COSMO_PACKAGE_ARCHIVE_CACHE" ] || [ -L "$COSMO_PACKAGE_ARCHIVE_CACHE" ]; then
    echo 'Compiler archive cache must be an ordinary directory' >&2
    exit 1
  fi
  setup_sha256
  verify_archive "$COSMO_PACKAGE_ARCHIVE_CACHE/aarch64-gcc.zip" "$ARM64_COMPILER_SHA256"
  verify_archive "$COSMO_PACKAGE_ARCHIVE_CACHE/x86_64-gcc.zip" "$AMD64_COMPILER_SHA256"
  verify_archive "$COSMO_PACKAGE_ARCHIVE_CACHE/llvm.zip" "$LLVM_COMPILER_SHA256"
fi

if ! MAKE=$(command -v gmake); then
  if ! MAKE=$(command -v make); then
    echo please install gnu make >&2
    exit 1
  fi
fi

$MAKE -j$NPROC m= \
  $APELINK $DEDUPE

if ! APE=$(command -v ape); then
  case $(uname -s) in
    Darwin)
      case $(mode) in
        aarch64)
          cc -O -o "$TMPDIR/ape.$$" .cosmocc/current/bin/ape-m1.c || exit
          trap 'rm "$TMPDIR/ape.$$"' EXIT
          APE=$TMPDIR/ape.$$
        ;;
        *) APE=.cosmocc/current/bin/ape-x86_64.macho ;;
      esac
      ;;
    *) APE=.cosmocc/current/bin/ape-$(uname -m).elf ;;
  esac
fi
stat $APE

$MAKE -j$NPROC m=$AMD64 \
  o/cosmocc.h.txt \
  o/$AMD64/ape/ape.lds \
  o/$AMD64/libc/crt/crt.o \
  o/$AMD64/libc/crt/crtfastmath.o \
  o/$AMD64/ape/ape.elf \
  o/$AMD64/ape/ape.macho \
  o/$AMD64/ape/ape.o \
  o/$AMD64/ape/ape-copy-self.o \
  o/$AMD64/ape/ape-no-modify-self.o \
  o/$AMD64/cosmopolitan.a \
  o/$AMD64/third_party/libcxx/libcxx.a \
  o/$AMD64/tool/build/assimilate.dbg \
  o/$AMD64/tool/build/march-native.dbg \
  o/$AMD64/tool/build/mktemper.dbg \
  o/$AMD64/tool/build/fixupobj.dbg \
  o/$AMD64/tool/build/tlscc.dbg \
  o/$AMD64/tool/build/zipcopy.dbg \
  o/$AMD64/tool/build/mkdeps.dbg \
  o/$AMD64/tool/build/zipobj.dbg \
  o/$AMD64/tool/build/apelink.dbg \
  o/$AMD64/tool/build/pecheck.dbg \
  o/$AMD64/tool/build/ar.dbg \
  o/$AMD64/tool/build/chmod.dbg \
  o/$AMD64/tool/build/cocmd.dbg \
  o/$AMD64/tool/build/compile.dbg \
  o/$AMD64/tool/build/mkdir.dbg \
  o/$AMD64/tool/build/cp.dbg \
  o/$AMD64/tool/build/echo.dbg \
  o/$AMD64/tool/build/gzip.dbg \
  o/$AMD64/tool/build/objbincopy.dbg \
  o/$AMD64/tool/build/package.dbg \
  o/$AMD64/tool/build/rm.dbg \
  o/$AMD64/tool/build/touch.dbg \
  o/$AMD64/tool/build/sha256sum.dbg \
  o/$AMD64/tool/build/resymbol.dbg \
  o/$AMD64/third_party/make/make.dbg \
  o/$AMD64/third_party/ctags/ctags.dbg

$MAKE -j$NPROC m=$AMD64-tiny \
  o/cosmocc.h.txt \
  o/$AMD64-tiny/ape/ape.lds \
  o/$AMD64-tiny/libc/crt/crt.o \
  o/$AMD64-tiny/libc/crt/crtfastmath.o \
  o/$AMD64-tiny/ape/ape.elf \
  o/$AMD64-tiny/ape/ape.macho \
  o/$AMD64-tiny/ape/ape.o \
  o/$AMD64-tiny/ape/ape-copy-self.o \
  o/$AMD64-tiny/ape/ape-no-modify-self.o \
  o/$AMD64-tiny/cosmopolitan.a \
  o/$AMD64-tiny/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$AMD64-sysv \
  o/cosmocc.h.txt \
  o/$AMD64-sysv/ape/ape.lds \
  o/$AMD64-sysv/libc/crt/crt.o \
  o/$AMD64-sysv/libc/crt/crtfastmath.o \
  o/$AMD64-sysv/ape/ape.elf \
  o/$AMD64-sysv/ape/ape.macho \
  o/$AMD64-sysv/ape/ape.o \
  o/$AMD64-sysv/ape/ape-copy-self.o \
  o/$AMD64-sysv/ape/ape-no-modify-self.o \
  o/$AMD64-sysv/cosmopolitan.a \
  o/$AMD64-sysv/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$AMD64-dbg \
  o/cosmocc.h.txt \
  o/$AMD64-dbg/ape/ape.lds \
  o/$AMD64-dbg/libc/crt/crt.o \
  o/$AMD64-dbg/libc/crt/crtfastmath.o \
  o/$AMD64-dbg/ape/ape.elf \
  o/$AMD64-dbg/ape/ape.macho \
  o/$AMD64-dbg/ape/ape.o \
  o/$AMD64-dbg/ape/ape-copy-self.o \
  o/$AMD64-dbg/ape/ape-no-modify-self.o \
  o/$AMD64-dbg/cosmopolitan.a \
  o/$AMD64-dbg/third_party/libcxx/libcxx.a \

$MAKE CONFIG_TARGET_ARCH= -j$NPROC m=$AMD64-optlinux \
  o/cosmocc.h.txt \
  o/$AMD64-optlinux/ape/ape.lds \
  o/$AMD64-optlinux/libc/crt/crt.o \
  o/$AMD64-optlinux/libc/crt/crtfastmath.o \
  o/$AMD64-optlinux/ape/ape.elf \
  o/$AMD64-optlinux/ape/ape.macho \
  o/$AMD64-optlinux/ape/ape.o \
  o/$AMD64-optlinux/ape/ape-copy-self.o \
  o/$AMD64-optlinux/ape/ape-no-modify-self.o \
  o/$AMD64-optlinux/cosmopolitan.a \
  o/$AMD64-optlinux/third_party/libcxx/libcxx.a \

$MAKE CONFIG_TARGET_ARCH= -j$NPROC m=$AMD64-tinylinux \
  o/cosmocc.h.txt \
  o/$AMD64-tinylinux/ape/ape.lds \
  o/$AMD64-tinylinux/libc/crt/crt.o \
  o/$AMD64-tinylinux/libc/crt/crtfastmath.o \
  o/$AMD64-tinylinux/ape/ape.elf \
  o/$AMD64-tinylinux/ape/ape.macho \
  o/$AMD64-tinylinux/ape/ape.o \
  o/$AMD64-tinylinux/ape/ape-copy-self.o \
  o/$AMD64-tinylinux/ape/ape-no-modify-self.o \
  o/$AMD64-tinylinux/cosmopolitan.a \
  o/$AMD64-tinylinux/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$ARM64 \
  o/$ARM64/ape/ape.elf \
  o/$ARM64/ape/aarch64.lds \
  o/$ARM64/libc/crt/crt.o \
  o/$ARM64/libc/crt/crtfastmath.o \
  o/$ARM64/cosmopolitan.a \
  o/$ARM64/third_party/libcxx/libcxx.a \
  o/$ARM64/tool/build/assimilate.dbg \
  o/$ARM64/tool/build/march-native.dbg \
  o/$ARM64/tool/build/mktemper.dbg \
  o/$ARM64/tool/build/fixupobj.dbg \
  o/$ARM64/tool/build/tlscc.dbg \
  o/$ARM64/tool/build/zipcopy.dbg \
  o/$ARM64/tool/build/mkdeps.dbg \
  o/$ARM64/tool/build/zipobj.dbg \
  o/$ARM64/tool/build/apelink.dbg \
  o/$ARM64/tool/build/pecheck.dbg \
  o/$ARM64/tool/build/ar.dbg \
  o/$ARM64/tool/build/chmod.dbg \
  o/$ARM64/tool/build/cocmd.dbg \
  o/$ARM64/tool/build/compile.dbg \
  o/$ARM64/tool/build/cp.dbg \
  o/$ARM64/tool/build/echo.dbg \
  o/$ARM64/tool/build/gzip.dbg \
  o/$ARM64/tool/build/objbincopy.dbg \
  o/$ARM64/tool/build/package.dbg \
  o/$ARM64/tool/build/rm.dbg \
  o/$ARM64/tool/build/touch.dbg \
  o/$ARM64/tool/build/mkdir.dbg \
  o/$ARM64/tool/build/sha256sum.dbg \
  o/$ARM64/tool/build/resymbol.dbg \
  o/$ARM64/third_party/make/make.dbg \
  o/$ARM64/third_party/ctags/ctags.dbg

$MAKE -j$NPROC m=$ARM64-tiny \
  o/$ARM64-tiny/ape/ape.elf \
  o/$ARM64-tiny/ape/aarch64.lds \
  o/$ARM64-tiny/libc/crt/crt.o \
  o/$ARM64-tiny/libc/crt/crtfastmath.o \
  o/$ARM64-tiny/cosmopolitan.a \
  o/$ARM64-tiny/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$ARM64-sysv \
  o/$ARM64-sysv/ape/ape.elf \
  o/$ARM64-sysv/ape/aarch64.lds \
  o/$ARM64-sysv/libc/crt/crt.o \
  o/$ARM64-sysv/libc/crt/crtfastmath.o \
  o/$ARM64-sysv/cosmopolitan.a \
  o/$ARM64-sysv/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$ARM64-dbg \
  o/$ARM64-dbg/ape/ape.elf \
  o/$ARM64-dbg/ape/aarch64.lds \
  o/$ARM64-dbg/libc/crt/crt.o \
  o/$ARM64-dbg/libc/crt/crtfastmath.o \
  o/$ARM64-dbg/cosmopolitan.a \
  o/$ARM64-dbg/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$ARM64-optlinux \
  o/$ARM64-optlinux/ape/ape.elf \
  o/$ARM64-optlinux/ape/aarch64.lds \
  o/$ARM64-optlinux/libc/crt/crt.o \
  o/$ARM64-optlinux/libc/crt/crtfastmath.o \
  o/$ARM64-optlinux/cosmopolitan.a \
  o/$ARM64-optlinux/third_party/libcxx/libcxx.a \

$MAKE -j$NPROC m=$ARM64-tinylinux \
  o/$ARM64-tinylinux/ape/ape.elf \
  o/$ARM64-tinylinux/ape/aarch64.lds \
  o/$ARM64-tinylinux/libc/crt/crt.o \
  o/$ARM64-tinylinux/libc/crt/crtfastmath.o \
  o/$ARM64-tinylinux/cosmopolitan.a \
  o/$ARM64-tinylinux/third_party/libcxx/libcxx.a \

mkdir -p "$OUTDIR/bin/"
cp tool/cosmocc/README.md "$OUTDIR/"
cp tool/cosmocc/LICENSE.* "$OUTDIR/"

mkdir -p "$OUTDIR/include/"
mkdir -p "$OUTDIR/include/libc/integral/"
cp -R libc/isystem/* "$OUTDIR/include/"
cp libc/integral/* "$OUTDIR/include/libc/integral/"
for x in $(cat o/cosmocc.h.txt); do
  mkdir -p "$OUTDIR/include/${x%/*}/"
  cp -f $x "$OUTDIR/include/${x%/*}/"
done

OLD=$PWD
cd "$OUTDIR/"
if [ ! -x bin/x86_64-linux-cosmo-gcc ]; then
  fetch "$ARM64_COMPILER_URL" "$ARM64_COMPILER_SHA256" &
  fetch_arm64_pid=$!
  fetch "$AMD64_COMPILER_URL" "$AMD64_COMPILER_SHA256" &
  fetch_amd64_pid=$!
  fetch "$LLVM_COMPILER_URL" "$LLVM_COMPILER_SHA256" &
  fetch_llvm_pid=$!
  fetch_status=0
  wait "$fetch_arm64_pid" || fetch_status=1
  wait "$fetch_amd64_pid" || fetch_status=1
  wait "$fetch_llvm_pid" || fetch_status=1
  if [ "$fetch_status" -ne 0 ]; then
    echo 'Compiler archive download or checksum verification failed' >&2
    exit 1
  fi
  unzip aarch64-gcc.zip &
  unzip_arm64_pid=$!
  unzip x86_64-gcc.zip &
  unzip_amd64_pid=$!
  unzip llvm.zip bin/clang-19 bin/clang-format &
  unzip_llvm_pid=$!
  unzip_status=0
  wait "$unzip_arm64_pid" || unzip_status=1
  wait "$unzip_amd64_pid" || unzip_status=1
  wait "$unzip_llvm_pid" || unzip_status=1
  if [ "$unzip_status" -ne 0 ]; then
    echo 'Compiler archive extraction failed' >&2
    exit 1
  fi
  rm -f aarch64-gcc.zip
  rm -f x86_64-gcc.zip
  rm -f llvm.zip
  mv bin/clang-19 libexec/clang  # use `cosmocc -mclang` instead
fi
rm -f bin/*-cpp
rm -f bin/*-gcc-*
rm -f bin/*-gprof
rm -f bin/*-strings
cd "$OLD"
$DEDUPE -s "$OUTDIR/"

for arch in $AMD64 $ARM64; do
  mkdir -p "$OUTDIR/$arch-linux-cosmo/lib/"
  mkdir -p "$OUTDIR/$arch-linux-cosmo/lib/dbg"
  mkdir -p "$OUTDIR/$arch-linux-cosmo/lib/tiny"
  mkdir -p "$OUTDIR/$arch-linux-cosmo/lib/sysv"
  mkdir -p "$OUTDIR/$arch-linux-cosmo/lib/optlinux"
  mkdir -p "$OUTDIR/$arch-linux-cosmo/lib/tinylinux"

  cp -f o/$arch/libc/crt/crt.o "$OUTDIR/$arch-linux-cosmo/lib/"
  cp -f o/$arch-dbg/libc/crt/crt.o "$OUTDIR/$arch-linux-cosmo/lib/dbg/"
  cp -f o/$arch-tiny/libc/crt/crt.o "$OUTDIR/$arch-linux-cosmo/lib/tiny/"
  cp -f o/$arch-sysv/libc/crt/crt.o "$OUTDIR/$arch-linux-cosmo/lib/sysv/"
  cp -f o/$arch-optlinux/libc/crt/crt.o "$OUTDIR/$arch-linux-cosmo/lib/optlinux/"
  cp -f o/$arch-tinylinux/libc/crt/crt.o "$OUTDIR/$arch-linux-cosmo/lib/tinylinux/"

  cp -f o/$arch/libc/crt/crtfastmath.o "$OUTDIR/$arch-linux-cosmo/lib/"
  cp -f o/$arch-dbg/libc/crt/crtfastmath.o "$OUTDIR/$arch-linux-cosmo/lib/dbg/"
  cp -f o/$arch-tiny/libc/crt/crtfastmath.o "$OUTDIR/$arch-linux-cosmo/lib/tiny/"
  cp -f o/$arch-sysv/libc/crt/crtfastmath.o "$OUTDIR/$arch-linux-cosmo/lib/sysv/"
  cp -f o/$arch-optlinux/libc/crt/crtfastmath.o "$OUTDIR/$arch-linux-cosmo/lib/optlinux/"
  cp -f o/$arch-tinylinux/libc/crt/crtfastmath.o "$OUTDIR/$arch-linux-cosmo/lib/tinylinux/"

  cp -f o/$arch/cosmopolitan.a "$OUTDIR/$arch-linux-cosmo/lib/libcosmo.a"
  cp -f o/$arch-dbg/cosmopolitan.a "$OUTDIR/$arch-linux-cosmo/lib/dbg/libcosmo.a"
  cp -f o/$arch-tiny/cosmopolitan.a "$OUTDIR/$arch-linux-cosmo/lib/tiny/libcosmo.a"
  cp -f o/$arch-sysv/cosmopolitan.a "$OUTDIR/$arch-linux-cosmo/lib/sysv/libcosmo.a"
  cp -f o/$arch-optlinux/cosmopolitan.a "$OUTDIR/$arch-linux-cosmo/lib/optlinux/libcosmo.a"
  cp -f o/$arch-tinylinux/cosmopolitan.a "$OUTDIR/$arch-linux-cosmo/lib/tinylinux/libcosmo.a"

  cp -f o/$arch/third_party/libcxx/libcxx.a "$OUTDIR/$arch-linux-cosmo/lib/"
  cp -f o/$arch-dbg/third_party/libcxx/libcxx.a "$OUTDIR/$arch-linux-cosmo/lib/dbg/"
  cp -f o/$arch-tiny/third_party/libcxx/libcxx.a "$OUTDIR/$arch-linux-cosmo/lib/tiny/"
  cp -f o/$arch-sysv/third_party/libcxx/libcxx.a "$OUTDIR/$arch-linux-cosmo/lib/sysv/"
  cp -f o/$arch-optlinux/third_party/libcxx/libcxx.a "$OUTDIR/$arch-linux-cosmo/lib/optlinux/"
  cp -f o/$arch-tinylinux/third_party/libcxx/libcxx.a "$OUTDIR/$arch-linux-cosmo/lib/tinylinux/"

  for lib in c dl gcc_s m crypt pthread resolv rt dl unwind gomp stdc++; do
    printf '\041\074\141\162\143\150\076\012' >"$OUTDIR/$arch-linux-cosmo/lib/lib$lib.a"
  done
  mkdir -p "$OUTDIR/lib/gcc/"
  touch "$OUTDIR/lib/gcc/libgomp.spec"  # needed if user passes -fopenmp but not -lgomp
done

cp -f o/$AMD64/ape/ape.o "$OUTDIR/x86_64-linux-cosmo/lib/"
cp -f o/$AMD64-dbg/ape/ape.o "$OUTDIR/x86_64-linux-cosmo/lib/dbg/"
cp -f o/$AMD64-tiny/ape/ape.o "$OUTDIR/x86_64-linux-cosmo/lib/tiny/"
cp -f o/$AMD64-sysv/ape/ape.o "$OUTDIR/x86_64-linux-cosmo/lib/sysv/"
cp -f o/$AMD64-optlinux/ape/ape.o "$OUTDIR/x86_64-linux-cosmo/lib/optlinux/"
cp -f o/$AMD64-tinylinux/ape/ape.o "$OUTDIR/x86_64-linux-cosmo/lib/tinylinux/"

cp -f o/$AMD64/ape/ape.lds "$OUTDIR/x86_64-linux-cosmo/lib/"
cp -f o/$AMD64-dbg/ape/ape.lds "$OUTDIR/x86_64-linux-cosmo/lib/dbg/"
cp -f o/$AMD64-tiny/ape/ape.lds "$OUTDIR/x86_64-linux-cosmo/lib/tiny/"
cp -f o/$AMD64-sysv/ape/ape.lds "$OUTDIR/x86_64-linux-cosmo/lib/sysv/"
cp -f o/$AMD64-optlinux/ape/ape.lds "$OUTDIR/x86_64-linux-cosmo/lib/optlinux/"
cp -f o/$AMD64-tinylinux/ape/ape.lds "$OUTDIR/x86_64-linux-cosmo/lib/tinylinux/"

cp -f o/$ARM64/ape/aarch64.lds "$OUTDIR/aarch64-linux-cosmo/lib/"
cp -f o/$ARM64-dbg/ape/aarch64.lds "$OUTDIR/aarch64-linux-cosmo/lib/dbg/"
cp -f o/$ARM64-tiny/ape/aarch64.lds "$OUTDIR/aarch64-linux-cosmo/lib/tiny/"
cp -f o/$ARM64-sysv/ape/aarch64.lds "$OUTDIR/aarch64-linux-cosmo/lib/sysv/"
cp -f o/$ARM64-optlinux/ape/aarch64.lds "$OUTDIR/aarch64-linux-cosmo/lib/optlinux/"
cp -f o/$ARM64-tinylinux/ape/aarch64.lds "$OUTDIR/aarch64-linux-cosmo/lib/tinylinux/"

cp -f o/$AMD64/ape/ape-no-modify-self.o "$OUTDIR/x86_64-linux-cosmo/lib/"
cp -f o/$AMD64-dbg/ape/ape-no-modify-self.o "$OUTDIR/x86_64-linux-cosmo/lib/dbg/"
cp -f o/$AMD64-tiny/ape/ape-no-modify-self.o "$OUTDIR/x86_64-linux-cosmo/lib/tiny/"
cp -f o/$AMD64-sysv/ape/ape-no-modify-self.o "$OUTDIR/x86_64-linux-cosmo/lib/sysv/"
cp -f o/$AMD64-optlinux/ape/ape-no-modify-self.o "$OUTDIR/x86_64-linux-cosmo/lib/optlinux/"
cp -f o/$AMD64-tinylinux/ape/ape-no-modify-self.o "$OUTDIR/x86_64-linux-cosmo/lib/tinylinux/"

cp -f ape/ape-m1.c "$OUTDIR/bin/"
cp -af tool/cosmocc/bin/* "$OUTDIR/bin/"
cp -f o/$AMD64/ape/ape.elf "$OUTDIR/bin/ape-x86_64.elf"
cp -f o/$AMD64/ape/ape.macho "$OUTDIR/bin/ape-x86_64.macho"
cp -f o/$ARM64/ape/ape.elf "$OUTDIR/bin/ape-aarch64.elf"

for x in assimilate march-native mktemper fixupobj zipcopy apelink pecheck mkdeps zipobj \
         ar chmod cocmd cp echo gzip objbincopy package rm touch mkdir compile sha256sum \
         resymbol tlscc; do
  $APE $APELINK \
    -l o/$AMD64/ape/ape.elf \
    -l o/$ARM64/ape/ape.elf \
    -M ape/ape-m1.c \
    -o "$OUTDIR/bin/$x" \
    o/$AMD64/tool/build/$x.dbg \
    o/$ARM64/tool/build/$x.dbg
done

for x in ar chmod cp echo gzip package rm touch mkdir compile sha256sum; do
  mv "$OUTDIR/bin/$x" "$OUTDIR/bin/$x.ape"
done

for x in make ctags; do
  $APE $APELINK \
    -l o/$AMD64/ape/ape.elf \
    -l o/$ARM64/ape/ape.elf \
    -M ape/ape-m1.c \
    -o "$OUTDIR/bin/$x" \
    o/$AMD64/third_party/$x/$x.dbg \
    o/$ARM64/third_party/$x/$x.dbg
done
