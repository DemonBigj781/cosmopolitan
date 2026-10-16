#!/usr/bin/env bash
# Stage-1 compiler payload experiment. This is NOT an in-process compiler.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${COSMOCC_ROOT:?Set COSMOCC_ROOT to extracted cosmocc SDK root}"
: "${COSMOCC:?Set COSMOCC to compiler binary}"
mkdir -p o/toolchain
"$COSMOCC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/dispatcher-embed.o
"$COSMOCC" -O2 -c toolchain/embedded-sdk-driver.c -o o/toolchain/sdk-driver.o
"$COSMOCC" -O2 -c toolchain/native_zip.c -I. -o o/toolchain/native-zip.o
"$COSMOCC" -O2 -c toolchain/machin_build.c -o o/toolchain/machin-build.o
"$COSMOCC" -O2 -c toolchain/rust_build.c -o o/toolchain/rust-build.o
# Optional self-hosted Machin compiler linked into the SAME APE.
if [[ -n "${MACHIN:-}" && -n "${MACHIN_SRC:-}" ]]; then
  sources=(selfhost/lex.src selfhost/parse.src selfhost/check.src
    selfhost/checkgen.src selfhost/cgen.src selfhost/cgbuiltin.src
    selfhost/alias.src selfhost/cgagg.src selfhost/cgffi.src
    selfhost/cgprelude.src selfhost/cgprog.src selfhost/compile.src
    selfhost/cgmain.src)
  (cd "$MACHIN_SRC" && "$MACHIN" encode "${sources[@]}") > o/toolchain/compiler.mfl
  test -s o/toolchain/compiler.mfl
  "$MACHIN" build o/toolchain/compiler.mfl --emit-c > o/toolchain/compiler.c
  test -s o/toolchain/compiler.c
  "$COSMOCC" -O2 -std=c11 -pthread -Dmain=machin_entry -c o/toolchain/compiler.c -o o/toolchain/compiler.o
  MACHIN_OBJECT=o/toolchain/compiler.o
else
  MACHIN_OBJECT=""
fi
# Use Cosmopolitan's own zlib build rules instead of compiling source ad hoc.
# The archive includes the SIMD and header configuration needed by this fork.
COSMO_MAKE="${COSMO_MAKE:-make}"
"$COSMO_MAKE" -j2 o//third_party/zlib/zlib.a
ZLIB_ARCHIVE="o/third_party/zlib/zlib.a"
if [ ! -f "$ZLIB_ARCHIVE" ]; then
  ZLIB_ARCHIVE="o//third_party/zlib/zlib.a"
fi
test -f "$ZLIB_ARCHIVE" || { echo "Cosmopolitan zlib archive missing" >&2; exit 1; }
# Build an ARM64 companion archive using Cosmopolitan's own Make rules.
# The multiarch cosmocc wrapper requires matching x86-64 and ARM64 archives.
"$COSMO_MAKE" -j2 MODE=aarch64 o/aarch64/third_party/zlib/zlib.a
ARM_ZLIB="o/aarch64/third_party/zlib/zlib.a"
test -f "$ARM_ZLIB" || { echo "ARM64 zlib archive missing" >&2; exit 1; }
mkdir -p o/third_party/zlib/.aarch64
cp "$ARM_ZLIB" o/third_party/zlib/.aarch64/zlib.a
"$COSMOCC" -O2 -o o/toolchain/toolchain.com \
  o/toolchain/dispatcher-embed.o o/toolchain/sdk-driver.o \
  o/toolchain/native-zip.o o/toolchain/machin-build.o o/toolchain/rust-build.o ${MACHIN_OBJECT:+"$MACHIN_OBJECT"} "$ZLIB_ARCHIVE"
mkdir -p o/toolchain/compiler-payload
python3 - "$COSMOCC_ROOT" "${RUST_APE_SOURCE:-}" "${RUST_COMPILER_ROOT:-}" "${RUST_APE_SDK_ROOT:-}" <<'PY'
import os,sys,zipfile,hashlib
root=os.path.realpath(sys.argv[1])
rust_source=os.path.realpath(sys.argv[2]) if sys.argv[2] else None
rust_compiler=os.path.realpath(sys.argv[3]) if sys.argv[3] else None
rust_ape_sdk=os.path.realpath(sys.argv[4]) if sys.argv[4] else None
if rust_ape_sdk and not os.path.isfile(os.path.join(rust_ape_sdk,'generated','x86_64-unknown-linux-musl.json')):
    raise SystemExit('RUST_APE_SDK_ROOT must contain generated/x86_64-unknown-linux-musl.json')
rust_ape_sdk=os.path.realpath(sys.argv[4]) if sys.argv[4] else None
if rust_ape_sdk and not os.path.isfile(os.path.join(rust_ape_sdk,'generated','x86_64-unknown-linux-musl.json')):
    raise SystemExit('RUST_APE_SDK_ROOT requires generated/x86_64-unknown-linux-musl.json')
if rust_compiler and not os.path.isfile(os.path.join(rust_compiler,'bin','rustc')):
    raise SystemExit('RUST_COMPILER_ROOT must contain bin/rustc')
if rust_source and not os.path.isfile(os.path.join(rust_source,'run.sh')):
    raise SystemExit('RUST_APE_SOURCE must point to third_party/rust_ape')
out='o/toolchain/compiler-payload/cosmocc-sdk.zip'
files=[]
for directory,_,names in os.walk(root):
    for name in names:
        path=os.path.join(directory,name)
        files.append(path)
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True,strict_timestamps=False) as z:
    for path in sorted(files):
        arc=os.path.relpath(path,root)
        if os.path.islink(path):
            import stat
            info=zipfile.ZipInfo(arc)
            info.create_system=3
            info.external_attr=(stat.S_IFLNK | 0o777)<<16
            z.writestr(info,os.readlink(path))
        else:
            z.write(path,arc)
    if rust_source:
        rust_count=0
        for directory, dirs, names in os.walk(rust_source):
            dirs[:] = sorted(d for d in dirs if d not in ('.git', 'target', 'o', '__pycache__'))
            for name in sorted(names):
                path=os.path.join(directory,name)
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                arc='rust-ape/'+os.path.relpath(path,rust_source)
                z.write(path,arc)
                rust_count+=1
        if not rust_count:
            raise SystemExit('Rust-APE source directory is empty')
        z.writestr('rust-ape/EMBEDDED.txt','Rust-APE source snapshot; bootstrap still requires external host prerequisites.\\n')
        print('Rust-APE source files packaged:',rust_count)
    if rust_compiler:
        count=0
        for directory, dirs, names in os.walk(rust_compiler):
            dirs.sort()
            for name in sorted(names):
                path=os.path.join(directory,name)
                arc='rust-toolchain/'+os.path.relpath(path,rust_compiler)
                if os.path.islink(path):
                    import stat
                    info=zipfile.ZipInfo(arc)
                    info.create_system=3
                    info.external_attr=(stat.S_IFLNK | 0o777)<<16
                    z.writestr(info,os.readlink(path))
                elif os.path.isfile(path):
                    z.write(path,arc)
                count+=1
        print('Rust compiler SDK entries packaged:',count)
    if rust_ape_sdk:
        count=0
        for directory, dirs, names in os.walk(rust_ape_sdk):
            dirs[:] = sorted(d for d in dirs if d not in ('.git','target','build','__pycache__'))
            for name in sorted(names):
                path=os.path.join(directory,name)
                arc='rust-ape-sdk/'+os.path.relpath(path,rust_ape_sdk)
                if os.path.islink(path):
                    import stat
                    info=zipfile.ZipInfo(arc)
                    info.create_system=3
                    info.external_attr=(stat.S_IFLNK | 0o777)<<16
                    z.writestr(info,os.readlink(path))
                elif os.path.isfile(path):
                    z.write(path,arc)
                count+=1
        print('Patched Rust-APE SDK entries packaged:',count)
    if rust_ape_sdk:
        count=0
        for directory, dirs, names in os.walk(rust_ape_sdk):
            dirs[:] = sorted(d for d in dirs if d not in ('.git', 'target', '__pycache__'))
            for name in sorted(names):
                path=os.path.join(directory,name)
                arc='rust-ape-sdk/'+os.path.relpath(path,rust_ape_sdk)
                if os.path.islink(path):
                    import stat
                    info=zipfile.ZipInfo(arc)
                    info.create_system=3
                    info.external_attr=(stat.S_IFLNK | 0o777)<<16
                    z.writestr(info,os.readlink(path))
                elif os.path.isfile(path):
                    z.write(path,arc)
                count+=1
        print('Patched Rust-APE SDK entries packaged:',count)
with zipfile.ZipFile(out) as z:
    assert z.testzip() is None
    assert any(os.path.basename(n)=='cosmocc' for n in z.namelist())
    if rust_source:
        assert 'rust-ape/run.sh' in z.namelist()
    if rust_ape_sdk:
        assert 'rust-ape-sdk/generated/x86_64-unknown-linux-musl.json' in z.namelist()
    if rust_ape_sdk:
        assert 'rust-ape-sdk/generated/x86_64-unknown-linux-musl.json' in z.namelist()
print('payload:',out,'files:',len(files),'bytes:',os.path.getsize(out))
PY
# Zip payload is appended to the existing APE; the APE runtime is expected
# to ignore the trailing bytes. Verify that assumption with a smoke test.
python3 - <<'PY'
import os,struct,zipfile
exe='o/toolchain/toolchain.com'
archive='o/toolchain/compiler-payload/cosmocc-sdk.zip'
out='o/toolchain/compiler-payload/toolchain-with-sdk.com'
with open(out,'wb') as dst:
    with open(exe,'rb') as src:
        while data:=src.read(1024*1024): dst.write(data)
    offset=dst.tell()
    with open(archive,'rb') as src:
        while data:=src.read(1024*1024): dst.write(data)
    length=dst.tell()-offset
    dst.write(struct.pack('<8sQQ',b'COSZIP01',offset,length))
os.chmod(out,0o755)
with open(out,'rb') as f:
    f.seek(-24,2)
    magic,start,size=struct.unpack('<8sQQ',f.read(24))
    assert magic==b'COSZIP01'
    f.seek(start)
    with open(archive,'rb') as z:
        assert f.read(size)==z.read()
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
print('PASS: standalone SDK archive and APE payload footer validated')
PY
o/toolchain/compiler-payload/toolchain-with-sdk.com selftest
if [[ -n "$MACHIN_OBJECT" ]]; then
  o/toolchain/compiler-payload/toolchain-with-sdk.com machin-compiler --full toolchain/machin_demo.mfl > o/toolchain/combined-machin-demo.c
  test -s o/toolchain/combined-machin-demo.c
  grep -q 'main' o/toolchain/combined-machin-demo.c
  echo 'PASS: self-hosted Machin and compiler SDK share one APE'
fi
echo 'NOTE: compiler payload is packaged, not executable through an internal compiler driver.'
