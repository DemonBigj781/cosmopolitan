#!/usr/bin/env bash
# Stage-1 compiler payload experiment. This is NOT an in-process compiler.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${COSMOCC_ROOT:?Set COSMOCC_ROOT to extracted cosmocc SDK root}"
test -f o/toolchain/toolchain.com
mkdir -p o/toolchain/compiler-payload
python3 - "$COSMOCC_ROOT" <<'PY'
import os,sys,zipfile,hashlib
root=os.path.realpath(sys.argv[1])
out='o/toolchain/compiler-payload/cosmocc-sdk.zip'
files=[]
for directory,_,names in os.walk(root):
    for name in names:
        path=os.path.join(directory,name)
        if os.path.islink(path): continue
        files.append(path)
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
    for path in sorted(files):
        z.write(path,os.path.relpath(path,root))
with zipfile.ZipFile(out) as z:
    assert z.testzip() is None
    assert any(os.path.basename(n)=='cosmocc' for n in z.namelist())
print('payload:',out,'files:',len(files),'bytes:',os.path.getsize(out))
PY
# Zip payload is appended to the existing APE; the APE runtime is expected
# to ignore the trailing bytes. Verify that assumption with a smoke test.
cp o/toolchain/toolchain.com o/toolchain/compiler-payload/toolchain-with-sdk.com
cat o/toolchain/compiler-payload/cosmocc-sdk.zip >> o/toolchain/compiler-payload/toolchain-with-sdk.com
chmod +x o/toolchain/compiler-payload/toolchain-with-sdk.com
o/toolchain/compiler-payload/toolchain-with-sdk.com selftest
python3 - <<'PY'
import zipfile
p='o/toolchain/compiler-payload/toolchain-with-sdk.com'
with zipfile.ZipFile(p) as z:
    assert z.testzip() is None
    assert any(n.endswith('/cosmocc') or n=='cosmocc' for n in z.namelist())
print('PASS: SDK zip payload readable from combined APE')
PY
echo 'NOTE: compiler payload is packaged, not executable through an internal compiler driver.'
