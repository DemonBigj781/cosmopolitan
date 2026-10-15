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
# Compile a minimal zlib implementation with the same Cosmopolitan toolchain.
ZLIB_SRC="adler32 crc32 inffast inflate inftrees zutil"
ZLIB_FLAGS="-DADLER32_SIMD_SSSE3 -DCRC32_SIMD_SSE42_PCLMUL -DCRC32_SIMD_AVX512_PCLMUL -DDEFLATE_SLIDE_HASH_SSE2 -DINFLATE_CHUNK_SIMD_SSE2 -DINFLATE_CHUNK_READ_64LE"
COSMO_X64="$(dirname "$COSMOCC")/x86_64-linux-cosmo-gcc"
if [ ! -x "$COSMO_X64" ]; then
  echo "Missing x86-64 Cosmopolitan compiler: $COSMO_X64" >&2
  exit 1
fi
ZLIB_OBJS=""
for unit in $ZLIB_SRC; do
  "$COSMO_X64" -O2 -I. $ZLIB_FLAGS -c "third_party/zlib/$unit.c" -o "o/toolchain/zlib-$unit.o"
  ZLIB_OBJS="$ZLIB_OBJS o/toolchain/zlib-$unit.o"
done
# Deliberate word splitting: object file list generated from fixed source names.
"$COSMOCC" -o o/toolchain/toolchain.com o/toolchain/dispatcher-embed.o o/toolchain/sdk-driver.o o/toolchain/native-zip.o $ZLIB_OBJS
mkdir -p o/toolchain/compiler-payload
python3 - "$COSMOCC_ROOT" <<'PY'
import os,sys,zipfile,hashlib
root=os.path.realpath(sys.argv[1])
out='o/toolchain/compiler-payload/cosmocc-sdk.zip'
files=[]
for directory,_,names in os.walk(root):
    for name in names:
        path=os.path.join(directory,name)
        files.append(path)
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
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
with zipfile.ZipFile(out) as z:
    assert z.testzip() is None
    assert any(os.path.basename(n)=='cosmocc' for n in z.namelist())
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
echo 'NOTE: compiler payload is packaged, not executable through an internal compiler driver.'
