# Unified Cosmopolitan APE roadmap

## Objective and priorities

Extend Cosmopolitan without replacing or regressing its existing capabilities.
The immediate milestone is a practical, self-contained C/C++/Rust/Machin
toolchain. Larger language and ML integrations follow that foundation.

Three separate acceptance levels apply:

| Level | Acceptance criterion |
| --- | --- |
| Toolchain | The same unified APE launches on the supported systems. |
| Compiler/runtime | Each embedded implementation actually works there. |
| Generated programs | Produced or hosted programs run as compatible APEs. |

Linux and Windows are primary targets. macOS is optional but desired: prepare
compatible artifacts where practical and distinguish build support from an
actual macOS runtime test. Track CPU architecture separately from operating
system. A successful launcher test does not establish the other two levels.

Future command examples such as `toolchain.com python hello.py -o hello.com`
are interface proposals, not existing commands or a finalized CLI contract.
Interpreted programs may embed code, libraries, and a Cosmopolitan-compiled
runtime; this is not a claim that all language code becomes native machine code.

## 1. Establish and preserve the baseline

- [x] Preserve starting commit `b2c79c6f` with local checkpoint tag
  `checkpoint/pre-sdk-cleanup-20261016`. This is not a fully green release tag.
- [x] Remove duplicate Rust-APE SDK traversal and validate unique archive paths.
- [x] Audit disposable build/download contents without removing required SDK
  resources, licenses, generated targets, or vendored source modules.
- [x] Build the full payload locally with pinned bootstrap dependencies and
  single-thread compilation controls.
- [x] Independently compile and execute C, C++17/STL/exceptions, Rust `no_std`
  with a C-runtime call, and Machin programs on Linux x86-64.
- [x] Add packaging/extraction regression tests and an independent smoke runner.
- [x] Fix the separate Rust linker workflow's missing ARM64 companion object;
  the hosted positive and unresolved-symbol checks now pass.
- [x] Preserve Linux CI-validated source `7dd60ac5` with local recovery tag
  `checkpoint/native-zip-linux-ci-20261016`. This is not a multi-platform release.

Local validation on October 16, 2026 used Cosmocc 4.0.2, Rust nightly-2026-07-28,
and Machin revision `9e0fb93e17c43d7ed047df80aa881037286aaae4`. A separate SDK copy
serialized Cosmocc's architecture workers. Tests used an explicit native APE
loader and a network-disabled namespace. The combined harness was interrupted
during Machin; a fresh independent Machin build-and-execute run passed.

The payload inventory had 14,265 unique entries and 76 symlinks, with no excluded
top-level SDK caches/build outputs. Both GCC and Clang passed 16 focused tests.
These results do not establish Windows/macOS execution, ARM64 execution, or a
relocatable Rust standard-library target. C++ emitted non-fatal SDK warnings.

### Hosted validation checkpoint

All 11 triggered workflows passed for commit
`7dd60ac5ee02e08ba345944f85f6a5478f2d80db`. The shared-artifact pipeline in run
`38962961798` passed packaging plus independent Linux C, C++, Rust, and Machin
compile-and-execute jobs. Each compiler job verified the same artifact hashes
before and after testing. Its native read-only status check passed in 0.014
seconds. The earlier CI loader-path error was corrected by keeping the loader
outside the test's replaced `/tmp` mount.

The CI artifact contains 10,587 resources, including the original executable
resources. Its staged host Rust compiler contains fewer files than the local
bootstrap sysroot; do not conflate that inventory with the local counts above.
The hosted workflows still have some unpinned upstream downloads, so fully
reproducible bootstrap inputs remain a separate task below.

## 2. Current milestone: self-contained existing toolchain

- [x] Prove native ZIPOS reads from the real SDK without extraction, using the
  existing `zipcopy` tool and a separate probe APE. Both resource checks passed
  on Linux with a read-only filesystem and read-only `/tmp`.
- [x] Implement native ZIP attachment while preserving existing executable
  resources, rejecting collisions, and publishing the output atomically.
- [x] Move `rust status` to ZIPOS without disk extraction. The new artifact passed
  a read-only-filesystem check in 0.036 seconds; invalid commands also fail before
  extraction. Compiler paths no longer create a separate copy of the SDK ZIP.
- [x] Re-run all four compiler smoke tests against the migrated format on hosted
  CI using the same downloaded artifact in separate jobs.
- [ ] Avoid redundant whole-SDK extraction for compiler subprocesses; explicitly
  determine what must exist on disk and manage temporary-file lifetimes.
- [ ] Make packaged Rust target/linker definitions relocatable at their source.
  Current generated definitions contain absolute build-machine paths.
- [ ] Audit shell utilities, dynamic libraries, loaders, compiler subprocesses,
  and build-time versus runtime dependencies. Packaged `rustc` and Cargo are
  currently Linux ELF binaries, not portable APE compiler implementations.
- [ ] Pin reproducible bootstrap inputs and record artifact checksums/licenses.
- [ ] Measure size, startup, resident memory, extraction I/O, and dependency
  count before optimizing. Preserve correct output and diagnostics.

The local staged SDK archive is about 1.17 GB compressed and 3.75 GB uncompressed.
The local migrated artifact adds the executable's three original resources, for
14,268 unique paths. Repeated compiler-side SDK extraction remains a measured
bottleneck, even though read-only status and the temporary ZIP copy are resolved.

## 3. Portability and regression gates

- [ ] Run the same artifact's compiler smoke tests on Linux and Windows.
- [ ] Add macOS builds/compatibility checks and runtime validation when available;
  macOS remains optional and must not block the primary targets unnecessarily.
- [ ] Keep launcher, compiler/runtime, and generated-program results distinct.
- [ ] Test absence of installed compilers, offline operation, negative inputs,
  filesystem isolation, and both architecture outputs where supported.
- [ ] Preserve the existing Rust/WebGPU/LLVM and software-driver paths.

Prefer GitHub-hosted Actions for heavyweight builds; keep local work focused on
editing and lightweight verification. Hosted runners may use their available
parallelism; local and self-hosted builds use one CPU thread.

The operator explicitly authorized pushing the current work to
`machin-cosmopolitan` and running its existing hosted workflows as an exception
to the main-branch-only rule. This does not authorize merging, releasing, or
changing the default branch (`master`). Other workflow scope changes still need
authorization.

## 4. Desired toolchain components

### sccache

- [ ] Integrate sccache as a desired source-preserving Rust component of the
  Cosmopolitan toolchain, not merely an assumed host-installed utility.
- [ ] Audit its Rust `std`, subprocess, filesystem, IPC, and dependency needs.
- [ ] Test cache hits/misses, invalidation, compiler identity, failure propagation,
  and equivalence of cached and uncached outputs.

The existing `cache` command is still a placeholder. The reserved xshell and
device-discovery commands also remain unimplemented; do not advertise them as
functional integrations.

## 5. Additional language ecosystems

Integration order: **Lua, Python, Ruby, then Mono/C#**.

- [ ] Inventory existing Cosmopolitan Lua/Ruby forks and ports before designing
  fresh ports. Check source/build recipes, licenses, maintenance, tests, and ABI
  compatibility; reuse suitable work rather than duplicating it.
- [ ] Integrate Lua's interpreter, bytecode engine, and required libraries.
  Existing vendored Lua/build targets are a starting point, not proof of unified
  dispatcher integration.
- [ ] Integrate CPython and its required libraries/resources. Audit the existing
  vendored CPython port and choose the supported version deliberately.
- [ ] Compare the supplied CPython port and RustPython references; assess PyO3
  interoperability and Cython/Nuitka compilation without assuming they eliminate
  the need for a compatible Python runtime.
- [ ] Integrate CRuby using suitable existing port work where feasible.
- [ ] Port the necessary Mono runtime/compiler infrastructure and managed
  resources. Validate AOT and JIT capabilities separately rather than assuming
  either is portable.

All four runtime implementations must participate in the Cosmopolitan/APE
ecosystem, not be ordinary host-dependent runtimes tucked inside an archive.

## 6. Optional ML/runtime integrations

### LibTorch: CPU and CUDA

- [ ] Assess LibTorch as an optional C++ integration, with **CPU and CUDA** as
  the intended backend targets. Do not silently redefine this as CPU-only.
- [ ] Audit C++ ABI, threading, allocators, operator dependencies, model formats,
  native driver loading, and the split between portable code and OS dependencies.
- [ ] Investigate **ZLUDA** as a candidate CUDA compatibility route, including
  upstream choices, supported devices, driver/runtime requirements, and the
  LibTorch operators/libraries actually needed. No compatibility is assumed.
- [ ] Verify numerical correctness and unavailable-backend diagnostics separately
  for CPU, native CUDA, and any viable ZLUDA route.

### Transformers/model-inference substitute

- [ ] Evaluate **Rustformers** as a candidate for the Transformers/model-inference
  role, identifying the exact project, maintenance state, supported models,
  weight formats, tokenizer support, licensing, and Cosmopolitan feasibility.
- [ ] If unsuitable, find a viable substitute for the required Transformers
  functionality. Distinguish model APIs/pipelines from the PyTorch/LibTorch
  tensor execution backend; do not assume a drop-in replacement for both.
- [ ] Define representative model-loading and inference tests before selecting
  or integrating an implementation.

### Preserve existing software compute

This branch already contains the Lavapipe/llvmpipe/LLVM software-compute
integration described in `third_party/wgpu_native/CPU.md`. Its recorded
Windows/Linux compute results are a separate baseline from the compiler tests.

- [ ] Preserve embedded software compute and existing native-provider behavior.
- [ ] Evaluate reuse of compatible LLVM/runtime work without assuming that
  software Vulkan/OpenGL makes CUDA or LibTorch operators compatible.
- [ ] Re-run the relevant numerical and dependency-isolation checks when these
  components are affected. Do not claim full graphics or ML conformance from
  the existing demonstration workloads.

## Scope guard

Record and investigate the later integrations now, but keep implementation
focused on section 2 until the current toolchain foundation is stable. Research
candidates are not selected dependencies or promises of supported platforms.

## Reuse-first reference map

These references were supplied by the operator and inspected at the overview
level. They are not automatically approved dependencies or verified Cosmo ports.
Before reuse, pin revisions and audit licenses, build recipes, architecture/ABI
assumptions, native extensions, and runtime dependencies.

### Cosmopolitan ecosystem and existing ports

- **[superconfigure][superconfigure] — high-priority reference.** Its port/build
  recipes are relevant across languages and host utilities. This branch already
  records its LLVM patch at revision
  `dd04d27c780832318962efbc6d140743ebff617b` in
  `third_party/llvm_cosmo/IMPORT.json`; inspect that existing integration before
  importing or rewriting equivalent work.
- **[cosmo.zip public catalog][cosmo-pub].** Inspect existing toolchain, runtime,
  and application artifacts alongside their source recipes. Verify checksums,
  licenses, architecture coverage, and provenance before reusing binaries.
- **[awesome-cosmopolitan][awesome-cosmo].** Use as a discovery index, then verify
  each candidate against its own source, tests, and supported versions.
- **[CPython `cosmo_py311` branch][cpython311].** Concrete CPython port reference;
  inspected branch tip was `c136d2d28bd35635395cdb958d93d5d3b65b4c3d`.
- **[cosmofy][cosmofy].** Python application packaging reference. Its README
  currently limits supported applications to pure Python without C extensions;
  this must not be mistaken for native-extension compatibility.
- **[cosmogfx][cosmogfx].** OpenGL/window-system interoperability proof of concept.
  Evaluate it separately from the existing headless WebGPU/software-compute
  baseline; its README does not establish macOS support.

Discovery leads from the curated index include [Cosmopolitan Lua][lua-cosmo],
[LuaJIT-cosmo][luajit-cosmo], and [mruby][mruby]. Verify their current compatibility
directly. mruby is not interchangeable with the agreed CRuby target; do not
silently substitute a reduced Ruby implementation.

### Python implementation, interoperability, and compilation

- **[RustPython][rustpython].** Alternative interpreter implementation reference;
  assess its Rust standard-library needs and Python compatibility before choosing
  it over CPython or changing the agreed language integration order.
- **[PyO3][pyo3].** Rust/Python bindings and embedding reference, not a replacement
  interpreter by itself. Test its interpreter-version and C-API assumptions.
- **[Cython][cython].** Python/Cython-to-C/C++ and extension-generation reference.
  Identify runtime and C-API requirements of the generated output.
- **[Nuitka][nuitka].** Python compilation and distribution reference. Its manual
  describes generated C using `libpython`; emitting C does not by itself remove
  the Python runtime requirement.

[superconfigure]: https://github.com/ahgamut/superconfigure
[cosmo-pub]: https://cosmo.zip/pub/
[awesome-cosmo]: https://github.com/shmup/awesome-cosmopolitan
[cpython311]: https://github.com/ahgamut/cpython/tree/cosmo_py311
[cosmofy]: https://github.com/metaist/cosmofy
[cosmogfx]: https://github.com/jacereda/cosmogfx
[lua-cosmo]: https://github.com/ahgamut/lua/tree/cosmopolitan
[luajit-cosmo]: https://github.com/ahgamut/LuaJIT-cosmo
[mruby]: https://github.com/mruby/mruby
[rustpython]: https://github.com/RustPython/RustPython
[pyo3]: https://github.com/PyO3/pyo3
[cython]: https://github.com/cython/cython
[nuitka]: https://github.com/Nuitka/Nuitka
