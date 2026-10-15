#!/usr/bin/env bash
# Stage-1 compiler payload experiment. This is NOT an in-process compiler.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${COSMOCC_ROOT:?Set COSMOCC_ROOT to extracted cosmocc SDK root}"
: "${COSMOCC:?Set COSMOCC to compiler binary}"
mkdir -p o/toolchain
"$COSMOCC" -O2 -c toolchain/ape_toolchain.c -o o/toolchain/dispatcher-embed.o
"$COSMOCC" -O2 -c toolchain/embedded-sdk-driver.c -o o/toolchain/sdk-driver.o
"$COSMOCC" -o o/toolchain/toolchain.com o/toolchain/dispatcher-embed.o o/toolchain/sdk-driver.o
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
