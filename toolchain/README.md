# Unified Cosmopolitan APE toolchain

**Target:** one final APE executable incorporating components authored in C,
C++, Rust and Machin. Transpilation is optional, not mandatory.

## Bootstrap milestone

`build.sh` builds the minimal single-binary command dispatcher. The separate
`package-compiler-sdk.sh` path packages compiler SDKs and can link the self-hosted
Machin compiler when `MACHIN` and `MACHIN_SRC` are supplied. The sccache, xshell,
and WebGPU discovery commands remain unimplemented and explicitly fail.

Build with a Cosmopolitan C compiler:

```sh
COSMOCC=/path/to/cosmocc sh toolchain/build.sh
```

Output: `o/toolchain/toolchain.com`.

## SDK packaging regression tests

Run `python3 -m unittest discover -s toolchain/tests -p 'test_*.py' -v` from the
repository root. This requires Python 3.11+, a host C compiler (`CC`, default `cc`),
and host zlib development headers/library. Tests compile small temporary host
harnesses serially; `TMPDIR` controls their temporary location.

The tests execute the packaging script's actual Python archive-generation block
with small fixture SDKs, then exercise the production native ZIP extractor
against host zlib. They verify unique paths, required resources, executable
permissions, pre-1980 timestamps, symlink extraction, unsafe-link rejection,
invalid inputs, and C-only packaging. They do **not** build the full toolchain
or establish APE portability on any operating system.

The patched Rust-APE SDK is traversed once. Its top-level `build`, `target`, `o`,
and `cache` directories are excluded as build/download artifacts, along with
`.git` and `__pycache__` directories throughout that SDK. Vendored sources,
generated target configuration, compiler resources, licenses, and nested source
directories named `build`, `target`, or `cache` remain included. Download-cache
exclusion does not promise offline re-bootstrap; this payload is for the staged
SDK. Duplicate archive paths are an error, not silently overwritten or deduplicated.

The packaging script uses single-job Make invocations. Compiler commands still
extract SDK files and invoke subprocesses, but read the ZIP directly from the
APE without making an intermediate ZIP copy. `rust status` uses ZIPOS and does
not extract files. Packaging a host compiler does not make it an APE compiler.

## Process-scoped SDK resources

C/C++ commands materialize only the base C SDK. Host `rustc`/Cargo commands
materialize only `rust-toolchain/`; source markers and capability checks stay in
ZIPOS. The patched SDK and source snapshot are not copied merely to run a host
compiler. `rust build` uses the SDK driver in-process, materializing Rust once
and C once instead of invoking four independently extracting copies of the APE.

`TMPDIR` selects the parent of private, invocation-owned temporary directories.
The parent must exist. SDK files and Rust/Machin intermediates are removed on
normal command completion, including compiler failures. Cleanup does not follow
symlinks, refuses a replacement root, and reports errors rather than silently
ignoring them. Abrupt termination such as SIGKILL can still leave scratch data;
there is no sweep of older directories and no persistent SDK cache. Small loader
caches owned by upstream tools are outside this cleanup contract.

Extraction rejects existing leaf files/symlinks and symlink parent directories.
Component-local aliases must stay within the selected component. Compiler output
files requested outside the scratch directories are retained.
Paths printed by compiler introspection refer to invocation-local SDK files;
external build systems requiring persistent SDK paths must use an explicitly
materialized SDK rather than depending on previously leaked temporary trees.

## Independent compiler smoke tests

The real-toolchain probes compile C, C++17 (including STL and exceptions), Rust
`no_std` with a C-runtime call, and the existing Machin sample. Each probe checks
the output's APE magic, executes it, and checks exact output and exit status.
A recognizable header alone is not proof of cross-platform portability.

<!-- markdownlint-disable-next-line MD013 -->
Run `python3 toolchain/tests/smoke_compilers.py --toolchain /path/to/toolchain.com --output-dir /path/to/new-results`.

Use `--language c`, `--language cpp`, `--language rust`, or `--language machin`
to test one backend independently. The Rust and Machin probes use the existing
`rust build` and `machin build` commands, not the proposed future CLI syntax.
Use `--ape-loader /path/to/ape-x86_64.elf` on Linux when direct APE execution is
not configured; the loader is applied to both the toolchain and generated output.
No system-wide executable handler needs to be installed for these tests.

The output directory must be new. Each language keeps its generated executable,
`build.log`, and `run.log`; `summary.json` records independent results. Languages
are tested serially, but compiler-internal parallelism must be constrained
separately when required (stock Cosmocc launches architecture workers in parallel).
Each probe uses its own temporary parent and also checks component materialization
counts and absence of owned temporary directories after completion. Use a
disk-backed temporary filesystem if the selected SDK exceeds available RAM.
`check_sdk_lifecycle.py` separately verifies cleanup after a real compiler error
while preserving unrelated caller files.

`test_smoke_runner.py` tests reporting and rejection of ordinary ELF output using
mocks. Those unit tests are not evidence that a real compiler works; run the
separate smoke command against the actual packaged artifact for that evidence.

## Native ZIP resource access

`attach_sdk.py` merges the SDK inventory with the executable's existing resources
in temporary build storage, then uses Cosmopolitan's `zipcopy` to write native
absolute ZIP offsets. It validates inventory, CRCs, and the original executable
prefix before atomically replacing the output. The standalone SDK archive and
input executable are not modified. Duplicate paths are errors rather than
silently overwriting symbol tables or other existing resources.

The custom `COSZIP01` footer is no longer emitted or consumed. Rebuild the
executable and payload together; the new driver has no legacy-footer fallback.
ZIP64 is not enabled: the native extraction/attachment tools have size and
record-count limits that must be audited before larger integrations are added.

`rust status` reads the source marker and checks resource permissions through
ZIPOS before creating any temporary directory. Its established "snapshot
extracted" status line refers to the marker decoded in memory; the following
line explicitly states that no SDK files were extracted. Capability lines report
packaged resources, not compiler execution or cross-platform compatibility.

On Linux, `tests/check_native_status.py` verifies full-payload status and invalid
argument handling under a read-only filesystem and read-only `/tmp` with network
access disabled. It requires `bwrap` and an explicit APE loader.
Both input executables must be outside `/tmp`, which the check replaces with
an empty read-only mount.

<!-- markdownlint-disable-next-line MD013 -->
Run `python3 toolchain/tests/check_native_status.py --toolchain /path/to/toolchain.com --ape-loader /path/to/ape-x86_64.elf`.

### Initial feasibility probe

`tests/zipos_probe.c` reads the real Rust source marker and patched target JSON
through `/zip/`. In the local Linux check, the probe failed before attaching
resources, then passed after the existing Cosmopolitan `zipcopy` tool attached
`o/toolchain/compiler-payload/cosmocc-sdk.zip`. It also passed with the filesystem
and `/tmp` mounted read-only and networking disabled: no SDK extraction was
needed for those resource reads.

This separate probe preceded the production attachment/status migration above.
The in-tree `tool/build/zipcopy.c` checks record counts and an `INT_MAX` output
size limit. Migration must also preserve access to any pre-existing embedded
resources; simply appending another archive is not a resource-merge strategy.

## Relocatable packaged Rust SDK definitions

`rust_sdk_resources.py` renders the packaged `generated/` definitions directly
from the pinned SDK's target templates. It does not modify the upstream snapshot
or perform runtime repair of extracted files. Linker, archiver, and C/C++ wrapper
scripts derive their SDK root from their own location. Both architecture target
JSON files use linker command names instead of build-machine absolute paths.

For a materialized SDK, `generated/cargo.cosmo.toml` supplies relative linker,
compiler, archiver, and crate-patch paths. Use that config with
the corresponding generated target JSON and the pinned nightly's existing
`-Z json-target-spec` and `-Z build-std` options. Direct `rustc` users instead need
the generated wrapper directory on `PATH`. The pinned Cargo resolves these
linker/env and `[patch]` paths from the config's grandparent. This is checked
directly with `check_cargo_config_paths.py` using offline Cargo metadata from an
unrelated directory; revalidate that behavior when changing the Cargo version.
The pinned Cargo reads its std-source override from its own environment, not
from `[env]` configuration intended for child processes. Before a std build,
set `__CARGO_TESTS_ONLY_SRC_ROOT` to the relocated SDK's `vendor/library` path.
The configuration probe also verifies that source selection using Cargo's unit
graph mode, without compiling any code.

The hosted relocation job exports the Rust and patched SDK components from the
same artifact onto a fresh runner, moves the root, and builds/runs a standard-
library application with no original SDK directory present. This is separate
from `rust build`'s current `no_std` interface and does not change `rust cargo`
into an implicit Cosmopolitan standard-library build command. The Rust compiler
itself remains a Linux-host binary; third-party tools retain their own path and
platform restrictions. Registry dependencies for a fresh std build are not yet
bundled for offline use.

## Next stages

The canonical priorities, completed baseline checks, and remaining work are in
[the repository roadmap](../roadmap.md). The current milestone is self-containment
of C/C++/Rust/Machin; later language and ML integrations must not displace it.

Do not claim source components are integrated until their tests run in the
combined APE.
