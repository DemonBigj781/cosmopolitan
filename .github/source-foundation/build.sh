#!/usr/bin/env bash
set -euo pipefail
root=$(pwd)
work="$RUNNER_TEMP/source-foundation"
mkdir -p "$work" "$root/ci-results" "$root/ci-artifact"
revision=5b25a74572b98a6a93dd2dd39c8396432187856f
bundle="$root/.github/source-foundation/fixes.bundle"
printf '%s  %s\n' d15b43c4c8e8095ad1573475a3905b587128e0e2f077fdf6f3ac3c3c76b92acd "$bundle" | sha256sum -c -
git bundle verify "$bundle"
git fetch "$bundle" HEAD
test "$(git rev-parse FETCH_HEAD)" = "$revision"
# Restore exact tested commit in an independent checkout; no CI files enter its identity.
git clone --no-checkout --no-hardlinks "$root" "$work/repository"
git -C "$work/repository" remote set-url origin https://github.com/DemonBigj781/cosmopolitan.git
git -C "$work/repository" fetch "$bundle" HEAD
git -C "$work/repository" checkout --detach "$revision"
python3 "$work/repository/toolchain/source_runtime.py" materialize --source "$work/repository" --revision "$revision" --output "$work/source"
python3 "$work/source/toolchain/bootstrap.py" sdk "$work/bootstrap-sdk" 2>&1 | tee "$root/ci-results/bootstrap.log"
python3 "$work/source/toolchain/source_runtime.py" build --source "$work/source" --bootstrap-sdk "$work/bootstrap-sdk" --output "$work/source-sdk" --jobs 2 2>&1 | tee "$root/ci-results/source-sdk.log"
python3 "$work/source/toolchain/source_runtime.py" verify --sdk "$work/source-sdk" > "$root/ci-results/source-sdk-verify.json"
# Explicit reviewed application extension, pinned to public source and checked independently.
curl -fLsS --retry 3 --connect-timeout 30 https://raw.githubusercontent.com/DemonBigj781/EasyDifussion-Cpp/24c86083148f37726a8ee1999c821734220c5259/cosmopolitan/backend/patches/0001-bounded-native-adapter-enumeration.patch -o "$work/adapter.patch"
printf '%s  %s\n' c0e164bdcf06faa800a6a500e8ae3e7c0ff3f41cf0be207771afc0e76a459c17 "$work/adapter.patch" | sha256sum -c -
cat > "$work/application-patches.json" <<'JSON'
{"schema_version":1,"patches":[{"name":"backend/patches/0001-bounded-native-adapter-enumeration.patch","path":"adapter.patch","sha256":"c0e164bdcf06faa800a6a500e8ae3e7c0ff3f41cf0be207771afc0e76a459c17"}]}
JSON
export COSMO_RUNTIME_PROFILE=source-built COSMO_RUNTIME_SDK="$work/source-sdk"
export WGPU_APPLICATION_PATCH_MANIFEST="$work/application-patches.json"
export CARGO_BUILD_JOBS=2 LLVM_COSMO_JOBS=2 LAVAPIPE_JOBS=2
cd "$work/source"
bash third_party/wgpu_native/build.sh --software --compile-only 2>&1 | tee "$root/ci-results/dependencies.log"
# CPU/software only; never selects a native hardware provider.
for workload in '--software' '--software --matmul' '--software --raster'; do
  timeout 300 o/webgpu/ape-x86_64.elf o/webgpu/webgpu_compute $workload 2>&1 | tee -a "$root/ci-results/software-smoke.log"
done
python3 toolchain/export_foundation.py --source-root "$work/source" --source-sdk "$work/source-sdk" --rust-sdk "$work/source/o/rust-ape/sdk" --output "$work/foundation-export" 2>&1 | tee "$root/ci-results/export.log"
tar -C "$work" -czf "$root/ci-artifact/foundation-export.tar.gz" foundation-export
cd "$root/ci-artifact"
sha256sum foundation-export.tar.gz > SHA256SUMS
printf '%s\n' "$revision" > FOUNDATION_REVISION
cp "$work/foundation-export/HANDOFF.json" "$work/foundation-export/PIN.candidate.json" .
cat SHA256SUMS
