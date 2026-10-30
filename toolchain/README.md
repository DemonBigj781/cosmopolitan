# Unified Cosmopolitan APE toolchain

**Target:** one final APE executable incorporating components authored in C,
C++, Rust and Machin. Transpilation is optional, not mandatory.

## Bootstrap milestone

`build.sh` builds the minimal single-binary command dispatcher, including the
existing Cosmopolitan Lua interpreter under the `lua` subcommand. The separate
`package-compiler-sdk.sh` path packages compiler SDKs and can link the self-hosted
Machin compiler when `MACHIN` and `MACHIN_SRC` are supplied. A verified native
sccache wrapper is staged for every packaged Rust host; xshell and WebGPU
discovery remain unimplemented and explicitly fail.

Build with a Cosmopolitan C compiler:

```sh
COSMOCC=/path/to/cosmocc sh toolchain/build.sh
```

Output: `o/toolchain/toolchain.com`.

The Lua dispatcher uses the in-tree Lua interpreter and its complete Cosmopolitan
library closure; it does not create a second Lua source tree. After a successful
build, verify it with:

```sh
o/toolchain/toolchain.com lua -e 'print(6 * 7)'
```

Expected output: `42`. The build scripts also perform this smoke test after
linking their minimal, Machin, self-hosted, and SDK-packaged APE variants.

## Pinned bootstrap and artifact provenance

The toolchain workflows share `.github/actions/toolchain-bootstrap`. Exact Go,
Machin commit, and standalone Rust linker-probe versions live in
`toolchain/bootstrap.lock.json`. Cosmocc's version/archive SHA256 and the Rust
nightly are read from the existing vendored Rust-APE definitions, not duplicated.
Run `python3 toolchain/bootstrap.py pins` to inspect the resolved pins.

`bootstrap.py sdk DESTINATION` invokes the existing checksum-checking downloader
in isolated temporary storage. `bootstrap.py machin SOURCE BINARY` fetches the
exact commit and checks the Go version before building. Both require new output
paths; neither overwrites nor silently trusts an existing installation.
`bootstrap.py rust DESTINATION` installs the pinned Linux x86-64/ARM64, macOS
x86-64/ARM64, and Windows x86-64 Rust/Cargo distributions into private rustup
storage, then stages one multi-host SDK. It never changes the user's default
toolchain or executes foreign-host compilers during staging. Distribution dates
and binary host headers are checked, and all required notices are preserved.
The same operation adds the pinned sccache release archives for those hosts,
checking each archive SHA256 before copying its native wrapper and license into
that host's sysroot. `bootstrap.py add-sccache DESTINATION` performs the same
addition for an already staged Rust SDK and refuses existing files.
`bootstrap.py stage-rust SYSROOT DESTINATION` can stage an existing single-host
sysroot, provided it contains both GNU/Linux ELF target standard libraries.
These helpers require Python 3.11+; installation also requires rustup.

SDK packaging adds `toolchain-metadata/BUILD.json` to the native ZIP and writes
external `BUILD.json` and `SHA256SUMS` beside `toolchain-with-sdk.com`. The embedded
record identifies the source commit/worktree state, requested pins, actual input
tree fingerprints, bootstrap checks, and Rust/Machin notice hashes. The sidecar
also records the final executable's size and SHA256, after verifying the packaged
notices. Machin's license accompanies its linked compiler; Rust notices retain
their `rust-toolchain/hosts/<host>/sysroot/share/doc/` paths.

Bootstrap stamps are local build records, **not signed attestations**. Packaging
rejects a stamped tree or Machin compiler that changed after bootstrap. Unstamped
user-provided inputs remain supported but are explicitly `unverified-local-input`;
requested pins do not certify them. Rust staging checks distribution dates and
native binary host formats,
not a fresh cryptographic validation of every installed component. Tree hashes
cover input names, modes, contents, and symlink targets without following links,
excluding only root `.git` and `.toolchain-bootstrap.json`. They describe inputs
before archive filtering and generation of relocatable configuration.

This pins bootstrap inputs, not the entire build environment: hosted runner
images, system packages, and rustup itself remain external inputs. It does not
establish bit-for-bit reproducible builds or additional OS execution support.

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

`rust sccache` runs the packaged native wrapper for the current host. `rust
cargo` automatically sets `RUSTC_WRAPPER` to that extracted native binary when
the caller has not already supplied a wrapper; an explicit `RUSTC_WRAPPER` is
preserved. The cache itself remains caller-owned through `SCCACHE_DIR`, and the
sccache server remains a native host process. This is intentionally separate
from `rust build`: that driver invokes direct `rustc --emit=obj -o ...` commands,
which do not match sccache's Cargo-oriented Rust wrapper contract. The attached
APE is not used as the Cargo wrapper because host `execve` handling for a
basename-only APE alias is not portable; Cargo receives the native host binary
instead.

Paths printed by compiler introspection refer to invocation-local SDK files;
external build systems requiring persistent SDK paths must use an explicitly
materialized SDK rather than depending on previously leaked temporary trees.

## Dependency audit

The 2026-10-21 audit separates bootstrap dependencies, toolchain execution
dependencies, and generated-program runtime dependencies. It used the packaged
payload, the source-level subprocess boundaries, explicit APE-loader probes,
and `strace -f -e trace=execve` on C, Rust, and Machin smoke builds.

### Toolchain execution

- The dispatcher, ZIPOS status path, and self-test run with an explicit native
  APE loader and `PATH=/nonexistent`. They do not require a host compiler,
  dynamic loader, shell utility, network connection, or SDK extraction for
  those commands. Direct execution without an APE handler is different: on
  Linux the kernel may fall back to the payload's shell-compatible launcher,
  which needs the host `sh` and `mkdir` before the APE starts. Use the explicit
  loader in isolated tests as documented below.
- C/C++ commands invoke the packaged `cosmocc` shell driver through `sh` on
  Unix or `sh.exe` on Windows. A normal Linux build exercised the host shell
  utilities `mkdir`, `rm`, `mv`, `dd`, `gzip`, `chmod`, and `uname`; `cat` is
  also required by the driver's non-terminal help path. The optional host `ape`
  command can replace per-tool APE extraction; otherwise the SDK's `.ape-1.10`
  scratch launcher is created in `TMPDIR`.
- The packaged compiler chain then invokes APE tools and target-specific
  compiler subprocesses: `x86_64-linux-cosmo-gcc`,
  `aarch64-linux-cosmo-gcc`, GCC `cc1`, `as`, `collect2`, `ld.bfd`, `apelink`,
  `fixupobj`, `mktemper`, and `pecheck`. These are packaged Cosmopolitan
  programs; the host dependencies above are the shell boundary around them.
- `rust build` invokes the packaged native `rustc` for both target objects,
  then the packaged C compiler for the wrapper and final APE link. `machin
  build` invokes the linked Machin compiler with `--full`, then follows the
  same C compiler path. Go is a bootstrap-time dependency for building the
  pinned Machin compiler, not a payload execution dependency.

### Native compiler and loader dependencies

The embedded Rust compiler, Cargo, and rustdoc remain native host programs,
not APE compilers. On Linux x86-64 they are dynamically linked ELF binaries
using `/lib64/ld-linux-x86-64.so.2`, the system `libc.so.6`, `libm.so.6`,
`libpthread.so.0`, `librt.so.1`, `libdl.so.2`, `libgcc_s.so.1`, and `libz.so.1`.
`rustc` additionally loads the packaged `librustc_driver` and `libLLVM` through
its relative `$ORIGIN/../lib` runpath. Other packaged host triples have their
own OS, architecture, loader, and system-library requirements. Bundling these
files does not remove those host requirements.
The packaged sccache binary has the same host-specific boundary; it needs the
selected host's native loader and libraries, a writable cache directory, and
its local server IPC. It is not an APE compiler or a portable replacement for
the host Rust compiler.

### Packaged Node.js companion

`toolchain.com node [arguments]` runs the packaged Node.js v26.13.0 executable
for the current host. The payload contains pinned official executables for Linux
x86-64/ARM64, macOS x86-64/ARM64, and Windows x86-64; Windows ARM uses the same
x64-emulation policy as the packaged Rust compiler. The dispatcher validates the
native executable format before creating a temporary directory, extracts only
the selected host component, forwards the Node exit status, and removes its owned
temporary files afterward. It never falls back to a system-installed `node`.

Stage the multi-host component with:

```sh
python3 toolchain/bootstrap.py nodejs /tmp/nodejs-embedded
```

Pass `NODEJS_ROOT=/tmp/nodejs-embedded` to `package-compiler-sdk.sh`, then run:

```sh
toolchain-with-sdk.com node -p '6 * 7'
```

Expected output: `42`. This packages the Node executable and its pinned upstream
license as a native toolkit companion. It does not port Node, V8, or libuv to
Cosmopolitan, and it does not currently package npm or Corepack.

The generated C, Rust, and Machin outputs are static Cosmopolitan APEs. Their
runtime portability therefore depends on an APE handler or an explicit native
loader, rather than on the Linux dynamic loader used by native Rust/Cargo.
Generated-program portability and compiler-host portability remain separate
contracts.

### Bootstrap and maintenance dependencies

Bootstrap and packaging retain their existing host prerequisites: Python 3.11+
for the helpers, `rustup` for pinned Rust distributions, `git` for the pinned
Machin checkout, Go at the pinned version for Machin compilation, a host C
compiler and zlib development files for the test/build harness, and `make` for
Cosmopolitan archive rules. Rust-APE maintenance scripts additionally use
`bash`, `python3`, and `perl`; they are not required by normal payload command
execution. Network access is needed to fetch bootstrap inputs, not to use an
already staged payload.

The audit does not claim a universal host-free compiler: the shell driver,
native Rust/Cargo distributions, loader setup, and bootstrap tools are explicit
external boundaries. It does establish that the packaged APE toolchain does not
silently fall back to an installed host compiler or host Rust toolchain during
the validated C, Rust, and Machin paths.

## Resource measurement baseline

The 2026-10-21 Linux x86-64 baseline used the explicit native APE loader, the
serial Cosmocc SDK, a warm local filesystem, and one compiler worker. These are
measurements for comparison before optimization, not universal performance
guarantees.

- **Final payload:** 1,191,436,238 bytes.
- **Embedded SDK archive:** 14,287 unique entries;
  1,189,347,842 compressed bytes and 3,801,429,171 uncompressed bytes.
- **`version` startup:** 0.0006 s median across 7 runs;
  0.0005–0.0263 s observed.
- **`selftest` startup:** 0.0005 s median across 7 runs;
  0.0005–0.0006 s observed.
- **`rust status` startup:** 0.0158 s median across 7 runs;
  0.0157–0.0161 s observed.
- **C smoke build:** 7.334 s and 186,596 KiB peak RSS. Its peak SDK
  footprint was 1,552,558,756 bytes across 3,010 files and 148 directories;
  the APE was 409,298 bytes.
- **Rust APE build:** 13.929 s and 323,644 KiB peak RSS. Its peak SDK
  footprint was 2,395,205,096 bytes across 6,851 files and 1,117 directories;
  the APE was 409,416 bytes.
- **Machin smoke build:** 6.863 s and 186,760 KiB peak RSS. Its peak SDK
  footprint was 1,552,558,756 bytes across 3,010 files and 148 directories;
  the APE was 731,071 bytes.
- **C extraction writes:** 1,428,755,991 bytes across 2,966
  `write`/`writev`/`pwrite64` calls to extracted SDK paths.

All three measured builds returned zero and left only the upstream `.ape-1.10`
scratch launcher in `TMPDIR`; the toolchain-owned SDK directories were removed.
The dependency-count view is intentionally layered: the archive contains
14,287 entries; `strace` observed 25 unique executable paths for C and 21 each
for Rust and Machin; the Linux C trace used nine host utility executables; and
the native Rust compiler set resolves to nine unique shared-library names,
including the packaged Rust driver/LLVM libraries and system libraries.

## Portability and regression gate evidence

The local Linux x86-64 gate was rerun on 2026-10-21 with the explicit APE
loader. A minimal `PATH` containing only the shell and the utilities needed by
the Cosmocc scripts contained no `cc`, `rustc`, `cargo`, or `go`; packaged C,
Rust, and Machin builds still returned zero and produced their expected APEs.
The same three builds passed inside `bwrap --unshare-net` with a writable
tmpfs and a bound output directory, demonstrating offline operation without
network access.

Negative probes covered an invalid Rust subcommand, missing C and Rust source
files, and malformed Machin input. Each returned nonzero, emitted diagnostics,
produced no output APE, and removed its owned temporary directories. The
existing `check_native_status.py` probe also passed with `/tmp` read-only,
network access disabled, and invalid-command handling; status completed in
0.044 seconds.

The Rust regression suite remains covered by the 116-test local suite. The
existing WebGPU ABI and loader checks passed with 27 ABI checks and 30 provider
isolation checks, and the LLVM/lavapipe source pins and metadata parse cleanly.
The rebuilt `o/webgpu/webgpu_compute` passed checksum, version, embedded
software-provider, Collatz, and 13x17-by-17x11 matrix checks through the
explicit APE loader. The separate chroot verifier reached its setup phase but
could not call `chroot` in this environment (`EPERM`), so isolated-root
verification remains an environment limitation rather than an assumed pass.

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
same artifact onto a fresh runner, moves the root, and cross-builds standard-
library programs with no original SDK directory present. This is separate
from `rust build`'s current `no_std` interface and does not change `rust cargo`
into an implicit Cosmopolitan standard-library build command. The Rust compiler
used by this relocation job is the Linux x86-64 member of the multi-host bundle;
third-party tools retain their own path and platform restrictions. Registry
dependencies for a fresh std build are not yet bundled for offline use.

## Rust-generated program portability

The current priority is portable **program output**, using the existing pinned
`starccy/rust-ape` SDK. This does not turn native `rustc` or Cargo executables into
APE binaries; native compiler-host payloads are handled separately below.

The Linux-hosted job builds both `x86_64-unknown-linux-musl` and
`aarch64-unknown-linux-musl` with the patched standard library. It reuses the
existing std smoke program and the pinned upstream std-only `threads.rs` example,
without copying or modifying that example or pulling in unrelated example
dependencies.

`link_rust_programs.py` follows the upstream two-architecture `apelink` recipe,
including both ELF loaders and the `ape-m1.c` bootstrap input. Requested inputs
are mandatory: there is no retry that silently omits one. Outputs preserve the
existing `rust-std.com` artifact name and add `rust-threads.com`, build metadata,
checksums, and the Rust-APE license. Build-recipe unit tests do not establish OS
compatibility.

The operator has now authorized cross-OS program execution tests in the existing
GitHub-hosted workflow. This supersedes the earlier macOS compile-only restriction
for these tests. Program-output results do not establish compiler-host portability
or a broad platform compatibility claim; compiler-host probes are separate below.

`tests/program_matrix.py` assembles the four existing compiler-smoke outputs and
the two Rust std/threading programs into one small, checksummed artifact. It
rejects failed build probes and mismatched Rust program hashes/source revisions.
The execution matrix consumes the same program bytes on Linux x86-64/ARM64,
macOS Intel/ARM64, Windows x86-64, and a Windows ARM64 host. Each job verifies the
actual host architecture, input hashes, process exit status, expected output, and
post-execution hashes. Windows copies receive an `.exe` suffix without changing
their contents. Windows PE machine metadata determines whether execution is
native or x64-emulated; an ARM runner alone does not establish native ARM output.

Linux and Intel macOS use the pinned SDK's explicit native loaders. Apple Silicon
compiles only the pinned `ape-m1.c` runtime loader using the runner's Apple C
compiler; the tested applications are not rebuilt. No systemwide APE handler is
installed. Program failures and timeouts remain failures, and later probes still
run. Per-program logs and `summary.json` are uploaded even when a test fails.
The build manifest deliberately does not claim runtime success; consult the
matching `program-results-*` artifacts for the actual execution results.

Xavier is an optional hardware-specific testing ground, not a build server or a
required CI runner. This matrix does not upgrade, configure, or clean that host.

## Compiler-host probes

`tests/compiler_hosts.py` verifies the packaged toolchain's identity, then checks
launcher commands and C/C++/Rust/Machin builds independently on each hosted target.
It reuses the same runtime loaders and compiler smoke samples rather than swapping
in host-installed compilers. Each supported compiler must produce an APE that runs
with the expected output and cleans its owned SDK temporary directories.

The C/C++ driver explicitly invokes the packaged `cosmocc` script through `sh`
on Unix or `sh.exe` on Windows, found on `PATH`. Native Windows process creation
does not interpret its shebang, and Cosmopolitan's executable lookup requires
the exact filename. Shell utilities remain host prerequisites, not bundled
capabilities. Drive-rooted script/file arguments use `C:/...` spelling at the
Windows shell boundary so native argument conversion cannot break the SDK's
slash-based directory calculations. Compiler flags and macro strings are not
rewritten.
The Windows shell also clears Cosmopolitan's private process/descriptor handoff
variables before sourcing the SDK script. Otherwise APE grandchildren can mistake
stale descriptor metadata for the shell's command-substitution pipes and corrupt
captured output. Ordinary environment variables and serialized resource-limit
policy are preserved.
Shell stdout/stderr on Windows are relayed through pipes to the parent so native
processes do not overwrite earlier diagnostics through incompatible log-file
offset handling. Both streams are drained concurrently; extraction-count checks
remain enabled rather than hiding missing log records.

The multi-host Rust payload keeps native compiler binaries, dynamic libraries,
host tools, installer metadata, and notices under
`rust-toolchain/hosts/<host>/sysroot`.
Identical target standard-library files are stored once under
`rust-toolchain/targets/<target>/lib`; each host sysroot has relative directory
aliases to them. Conflicting duplicate files fail staging rather than being
silently replaced. Windows DLLs remain beside their original executables.
The actual sysroot directory must not be named exactly like the host triple:
the pinned compiler treats that name specially when deriving its default sysroot
from the driver library. The explicit `sysroot` subdirectory preserves ordinary
Cargo discovery without injecting sysroot flags. Native helper tools retain their
original placement and dynamic-library paths inside each sysroot.
The ZIP stores shared libraries before host sysroots. Extraction selects only the
current host's compiler tree and helpers plus shared target data. Relocation
preserves those aliases. `hosts.json` describes the full packaged inventory.

`rust status` reports the selected compiler host. The dispatcher validates ELF,
Mach-O, or PE headers before extraction, uses the selected sysroot, and directs
Cargo's `RUSTC`/`RUSTDOC` to packaged tools. Windows ARM uses the x64 Windows
payload under emulation, matching the current APE PE member. Missing or mislabeled
payloads fail explicitly; there is no host-installed Rust fallback. These are
native compiler distributions packaged together, not a port of rustc to APE.
On Windows the shell waits for native Rust/Cargo as a foreground child, rather
than handing off its identity with `exec`, before the parent removes the SDK.
Cleanup errors still fail the command and identify the owned path involved.
On Windows only, regular `.exe`/`.dll` files denied deletion immediately after
execution have a shared, approximately five-second release budget. No permissions
or ACLs are changed, other file types/platforms are unaffected, and permanent
denials still fail cleanup. Release waits are logged rather than hidden.

`rust build` explicitly emits x86-64 and ARM64 GNU/Linux ELF objects on every
compiler host before Cosmopolitan links them. It no longer assumes the compiler
host is also the desired object target. The separate Cargo probe checks a std-
using library offline with a fresh Cargo home and requires actual metadata output.
It does not claim that ordinary `rust cargo build` implicitly creates an APE.

CI now requires real Rust builds and Cargo checks on every runner; the former
unsupported-host rejection is no longer accepted as a successful host contract.
Launcher, compiler, Cargo, and generated-program records remain distinct. Native
archive attachment still has a 2 GiB limit and fails before publication if exceeded;
the shared library layout avoids duplicating the same target payload per host.

## Next stages

The canonical priorities, completed baseline checks, and remaining work are in
[the repository roadmap](../roadmap.md). The current milestone is self-containment
of C/C++/Rust/Machin; later language and ML integrations must not displace it.

Do not claim source components are integrated until their tests run in the
combined APE.
