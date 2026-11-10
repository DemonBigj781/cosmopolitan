# Completed source foundation export

`export_foundation.py` is a host-side read/copy/hash exporter for the optional
Easy Diffusion source-runtime consumer. It does not build missing inputs, run
SDK programs, launch probes, infer a model, activate an application pin, start
CI, or use paid resources. This checkpoint contains implementation and host-only fixture verification.
**No real SDK export, application acceptance, dependency build or runtime/model
behavioral test has been performed.**

## Required completed inputs

Use one clean, committed foundation checkout, a whole source SDK produced by
`source_runtime.py`, its bound Rust SDK, and completed application-patched WGPU,
LLVM and Mesa outputs. The current WebGPU build layout is `o/webgpu`. An example
interface for a later authorized export is:

```sh
python3 toolchain/export_foundation.py \
  --source-root /work/cosmopolitan-foundation \
  --source-sdk /work/source-sdk \
  --rust-sdk /work/cosmopolitan-foundation/o/rust-ape/sdk \
  --output /exports/foundation-candidate
```

The output must not exist. Its parent must already exist, and the output must
be outside the source checkout and both SDK trees. Publication currently uses
Linux `renameat2(RENAME_NOREPLACE)` and fails closed if that primitive is not
available. Input trees must remain trusted and quiescent throughout export and
subsequent consumption. No hash manifest provides a security boundary against
hostile concurrent writers.

The SDK must have the new complete `SOURCE_INPUTS.json` evidence bound by
`SOURCE_RUNTIME.json`. Legacy source SDKs without that evidence are rejected.
The complete tracked path/type/Git-mode/size/SHA256 inventory must match the
selected committed checkout, index and Git blob bytes. The SDK repository,
revision and Git tree must match that same checkout. The origin must be an
HTTPS repository URL. Ignored build outputs are not tracked source inputs.

`source_patches: []` means **no additional runtime patch series after the pinned
commit**, not that the repository has no changes relative to an upstream
project. Accordingly, `runtime.provenance.patches_sha256` is the canonical JSON
SHA256 of the empty list. Imported/foundation/application dependency patches
retain their distinct detailed producer receipts.

The exporter requires the checked preparation receipt and actual copied
application patch bytes, including
`backend/patches/0001-bounded-native-adapter-enumeration.patch`. It does not take
a caller-supplied patch hash as evidence that a patch was applied. WGPU's
completed record must bind unchanged prepared trees, Cargo invocation, successful
Cargo artifact selection, build command records, Rust target, runtime and
staticlib hash. The Rust target label and preserved specification must match
the actual target selected by the verified Cargo invocation. LLVM target labels
must match the committed producer pin. The raw records are preserved rather
than rewritten.

LLVM's complete `archive_sha256`, Mesa's complete `link_archive_sha256`, exact
nested LLVM/Mesa metadata, current producer recipe/patch/version inputs and all
bound Rust shims are checked before and after staging. Unknown link switches,
shared/thin archives, stale records, generic unpatched WGPU outputs, dirty
sources, partial SDKs and source/stock identity mixing fail instead of falling
back. All verification is static; successful receipts do not prove codegen,
ABI correctness, numerical behavior or archive-member survival under LTO/GC.

## Output closure

- `sdk/`: the entire compiler/runtime package. Relative file and directory
  aliases are expanded using the original inventory, with target file modes
  preserved. Cycles, dangling links, escapes and special files fail. The
  original `SOURCE_RUNTIME.json` bytes are unchanged.
- `dependencies/`: verified ordinary archives, exact source-bound shim objects,
  include/runtime source closure and notices from `stage_webgpu_sdk`. Extra
  staged source files remain inventoried, but the application compiles only
  `vulkan_loader.c` and `win64_bridge.c`. Mesa registration is delivered by its
  prebuilt registration archive, not rebuilt with a potentially different
  version macro.
- `provenance/`: unchanged WGPU BUILD, PREPARED, Cargo/command/configuration
  records, copied application patches, original LLVM/Mesa LINK records, Rust
  profile/binding/target/table records, and generated component references.
- `dependencies/LINK.json`: `path_mode: "export-root-relative"`; every active
  SDK/archive/include/runtime/object/license path and `file_sha256` key is
  export-relative. License hash keys are relative to the license directory.
  Nested producer metadata retains its original paths as historical evidence.
- `HANDOFF.json`: strict Easy Diffusion handoff schema version 1, whole-SDK
  artifact inventory, source headers, seven exact compiler-tool paths and
  explicit default x86-64 CRT/libcosmo/libcxx inputs. The runtime identity covers
  all remaining SDK files through `toolchain.support_files`.
- `PIN.candidate.json`: actual repository/revision and the SHA256 of streamed
  `git archive --format=tar REVISION .` bytes, with `paths: ["."]`, completed
  producer versions and `cosmocc_version: "source-built"`.

Empty SDK `.a` linker-name stubs deliberately supplied by `package.sh` are
support **data**, never runtime/component archives. Delivered archive inputs
must be nonempty complete ordinary ar containers. Rust standard-library delivery
is explicitly `embedded-in-wgpu`: both Rust and WebGPU components reference the
same selected WGPU staticlib, and the Rust component provenance contains exactly
`archive_delivery: {mode: "embedded-in-wgpu", container_sha256: ...}`. Cargo/Rust
build inputs in provenance are not relabeled as separate final link archives.

## Consumer activation is separate

The application consumer can verify the export only after its reviewed pin
matches this candidate. The existing committed stock pin is intentionally
incompatible and is not changed by export. Do not copy the candidate into the
legacy stock preparation flow: the full `paths: ["."]` source archive requires
its own isolated source/output handling. Promotion, default routing, full
application builds and CPU/GPU qualification remain separate gates.

The exporter has no runtime dependency on an Easy Diffusion checkout. Focused
fixtures are in `toolchain/tests/test_export_foundation.py`. All 16 passed in
the implementation workspace, including the separate consumer schema check.
Optional fixtures use `EASY_DIFFUSION_CONSUMER_DIR` to locate the separate
consumer's host-only validator and selector. Synthetic exported bytes pass
both with the generated candidate pin, remain valid after relocation, and fail
with a different pin or changed SDK closure. This is contract compatibility,
not acceptance of a real source SDK or evidence of compiler/runtime behavior. Syntax-only checks and static
review must not be reported as behavioral test passes.
