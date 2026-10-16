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
repository root. This requires a host C compiler (`CC`, default `cc`) and host
zlib development headers/library. Tests compile one temporary host executable
serially; `TMPDIR` controls its temporary location.

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
The current compiler paths repeatedly extract large SDKs without cleanup; use
an isolated, disk-backed temporary filesystem for full-payload testing rather
than exhausting a RAM-backed `/tmp`.

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

## Next stages

The canonical priorities, completed baseline checks, and remaining work are in
[the repository roadmap](../roadmap.md). The current milestone is self-containment
of C/C++/Rust/Machin; later language and ML integrations must not displace it.

Do not claim source components are integrated until their tests run in the
combined APE.
