# WebGPU inside a Cosmopolitan executable

For the `webgpu-cpu` branch's static Mesa/LLVM CPU-driver experiment, see
[Embedded CPU Vulkan](CPU.md). Its identical executable has passed native
Windows and isolated Linux CPU compute in CI, and the current source adds a
verified offscreen triangle raster/readback path. The historical artifact and
exact checksum are recorded there. That validation is separate from the
external-driver baseline described below.

This experimental branch builds a **C application and wgpu-native's Rust
implementation into one x86-64 Actually Portable Executable (APE)**. The selected
backend is Vulkan. It does not bundle separately compiled Windows and Linux
copies of wgpu-native.

The initial program is a headless compute and raster check. CI has compiled one executable
on Linux, then run that exact file on **both Windows and Linux**. Both compute
jobs compiled the embedded WGSL shader, dispatched four workgroups, and
verified every result through Mesa lavapipe. The `--raster` workload also draws
an offscreen RGBA8 triangle and validates mapped texture pixels. Startup and full compute are
separate checks, and the executable's hash remains unchanged after each run.
Lavapipe is a CPU/software adapter; physical GPU acceleration still needs
hardware tests. This is an experimental compute foundation for an AI runtime.

## Build

Use a Linux x86-64 build host with Python 3.12 or later. On Ubuntu 24.04:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential ca-certificates clang curl git libclang-dev \
  patch pkg-config python3 unzip

bash third_party/wgpu_native/build.sh
```

The first build downloads the pinned compilers and Rust dependencies. All
generated files live under `o/`. The build uses Rust `nightly-2026-07-28`,
Cosmocc **4.0.2**, and the patched Rust standard library from `rust-ape`.
This target explicitly uses that SDK; it does not rebuild the repository's
ordinary Cosmocc 3.9.2 toolchain. Expect several gigabytes of build/cache space.

The build produces:

| Output | Purpose |
| --- | --- |
| `o/webgpu/webgpu_compute` | The x86-64 C/Rust/WebGPU/Vulkan APE |
| `o/webgpu/ape-x86_64.elf` | Matching explicit Linux loader, useful where APE is not registered |
| `o/webgpu/BUILD.json` | Source/toolchain revisions, lockfile hash, executable hash |
| `o/webgpu/SHA256SUMS` | Identity checks before and after execution |
| `o/webgpu/licenses/` | Dependency license notices for redistribution |
| `o/rust-ape/build/x86_64-unknown-linux-musl/release/libwgpu_native.a` | Cosmopolitan-compatible static library for the final link |

The sample build measured **5,775,216 bytes** for the executable. Its size and
hash can change with later source edits; use the manifest from your build.

If libclang is installed outside the system search path, set `LIBCLANG_PATH`
to the directory containing its shared library. Bindgen reads Cosmopolitan's
headers and normalization include, so it does not infer C types from a
different host runtime.

Application-specific native API patches can be supplied explicitly using
`WGPU_APPLICATION_PATCH_MANIFEST`. The default remains unmodified by application
extensions. See [application patches and receipts](APPLICATION_PATCHES.md) for
the input schema, prepared-source identity, Cargo/link evidence and declared
Rust standard-library delivery contract.

## Run

On Linux, check the C/Rust API without loading Vulkan:

```sh
bash third_party/wgpu_native/test.sh --version
```

For compute, install a host Vulkan loader and driver. A software driver is
enough to check functionality:

```sh
sudo apt-get install -y build-essential libvulkan1 mesa-vulkan-drivers
bash third_party/wgpu_native/test.sh
```

The script uses the explicit APE loader and checks the executable's hash before
and after execution. It imposes a process timeout in addition to the sample's
bounded callback polling. To choose a particular Vulkan implementation, set
`VK_DRIVER_FILES` to the driver's ICD JSON file before running.

On Windows x86-64, copy the **same** `webgpu_compute` and run in PowerShell:

```powershell
.\webgpu_compute --version
.\webgpu_compute
.\webgpu_compute --raster
```

The compute command needs a compatible installed Vulkan loader/driver. The
branch's CI downloads the one Linux build artifact for independent Linux and
Windows startup tests, and separately tests compute on both systems using
lavapipe. All four runtime checks have passed. The Windows compute job downloads
checksum-pinned Mesa and LunarG runtime components; it does not rebuild wgpu.
Set `COSMO_WGPU_TRACE=1` for stage markers and WebGPU debug messages. The
sample also enables Cosmopolitan crash reports, and CI retains its matching
`.com.dbg` image with the build diagnostics.

## What has been measured

Local and CI validation on 2026-10-06:

| Check | Result | What it establishes |
| --- | --- | --- |
| Patched Rust std + `parking_lot` APE | Passed on Linux | Runtime linking and threaded synchronization |
| Native call adapter tests | 27 passed with host GCC `ms_abi` functions | Scalar register/stack conversion, mixed floating point, resolver contexts, and rejection cases |
| Full C + Rust + WebGPU build | Passed | A statically linked WebGPU application packaged as x86-64 APE/PE |
| `--version` | Passed | Calls the actual Rust `wgpuGetVersion`, returning `29.0.1.1` |
| WebGPU shader dispatch/readback | Passed on Linux lavapipe | Buffer upload, WGSL compilation, Vulkan execution, synchronization, and exact output |
| Executable hash before/after both runs | Unchanged | Both checks executed the same file without rewriting it |
| Same-artifact Windows startup in CI | Passed | The Linux-built executable calls the embedded Rust WebGPU API on Windows with its hash unchanged |
| Same-artifact Windows WebGPU compute in CI | Passed on Windows lavapipe | Native driver calls, WGSL compilation, dispatch, synchronization, and verified readback from the Linux-built APE |
| Physical GPU acceleration | Pending | Software Vulkan does not establish hardware support or performance |

The successful compute run reported:

```text
wgpu-native 29.0.1.1 (0x1d000101)
C -> Rust version check: PASS
adapter: llvmpipe (LLVM 20.1.2, 256 bits); Mesa 25.2.8-0ubuntu0.24.04.2 (LLVM 20.1.2); backend=6 (Vulkan=6); type=CPU/software
input: [1, 2, 3, 4]
readback: [0, 1, 7, 2]
headless Vulkan compute and readback: PASS
```

The complete [CI run 37477427021](https://github.com/DemonBigj781/cosmopolitan-lua/actions/runs/37477427021)
passed all five jobs at implementation commit
`2a16f968e3a2081d2dd227d3917882c0a7f5d7c7`. The Windows compute run reported Mesa
26.1.3 / LLVM 22.1.8 and the same `[0, 1, 7, 2]` readback. Its application was the
one [build artifact](https://github.com/DemonBigj781/cosmopolitan-lua/actions/runs/37477427021/artifacts/11420085681)
used by every runtime job, with SHA-256:

```text
0e38dc8593e2e686da657a113293e3418d74598e04d149c41fd97fbc07426df3
```

The native Windows thread stack correction below resolved the previously
observed access violation in this tested configuration. These software-driver
results do not establish a physical GPU vendor compatibility matrix.

## How the integration works

1. `upstream/` contains **wgpu-native v29.0.1.1** and its exact WebGPU header
   submodule as ordinary source files. The implementation stays in Rust; the
   application calls its C interface.
2. `../rust_ape/` supplies the experimental Rust/Cosmopolitan standard-library
   port. It rebuilds `std`, patches `libc` and `errno`, and links runtime shims
   so a Linux compile-time personality does not directly issue Linux-only
   operations when the executable runs on another OS.
3. `prepare.py` checks the pristine source hashes, downloads checksum-pinned
   ash and wgpu-hal crates, and applies the small reviewed patches under `o/`.
   `Cargo.cosmo.lock` pins the modified graph. The original upstream lockfile
   remains available for comparison.
4. The ash patch uses `runtime/vulkan_loader.c`. It chooses `vulkan-1.dll` or
   `libvulkan.so.1` at runtime with Cosmopolitan's native-library API.
5. The loader adapts **every function returned by both Vulkan procedure
   resolvers**, including device-specific addresses. Linux calls use
   `cosmo_dltramp` for the foreign runtime boundary. Windows uses a generated
   scalar call adapter with metadata for **684** procedures from the exact ash
   dispatch tables. The Windows adapter handles mixed integer/float arguments
   and stack arguments; a generic integer-only cast would not suffice.
6. The C smoke program embeds compute, vertex, and fragment WGSL. It checks
   numerical buffer results and an offscreen RGBA8 triangle readback. It needs
   no browser, window, display server, or external shader file.
7. `configure_pe.py` raises the generated executable's Windows native thread
   stack reservation from the SDK's 64 KiB to 8 MiB, retaining the 4 KiB initial
   commitment. This accommodates driver-created threads that inherit PE stack
   defaults instead of using Cosmopolitan's separately allocated stacks. It
   runs before checksums are recorded, so both OSes receive the same bytes.

## Native-provider baseline boundaries

- **CPU/OS scope:** x86-64 Windows and Linux are the intended targets. This
  integration does not claim ARM64, macOS/Metal, Direct3D 12, browser execution,
  or universal OS support.
- **Native callbacks:** Vulkan debug/validation callbacks need a reverse ABI
  and foreign-thread TLS design. The patch rejects debug/validation flags and
  custom native instance callbacks before loading Vulkan. The sample uses
  flags zero. WebGPU's normal API validation remains enabled.
- **Headless scope:** basic offscreen triangle rasterization and texture
  readback are covered. Surfaces, swapchains, presentation, external resource
  sharing, and broader multithreaded driver behavior require further tests.
- **Host dependency:** The native provider uses OS-specific Vulkan loaders and
  drivers. The separate `--software` build and its embedded driver are described
  in [CPU.md](CPU.md).
- **Linux loader helper:** Cosmopolitan 4.0.2's `cosmo_dlopen` invokes the host
  `cc` to compile a small native helper on first use. Therefore this version
  does **not** yet prove deployment to a compiler-free Linux installation.
  The application and wgpu implementation are built once; the native helper
  is a remaining runtime packaging issue.
- **Experimental runtime:** `rust-ape` is a community port, with its own
  documented limitations. Building with a normal Linux Rust toolchain and
  merely renaming its library would not provide the same runtime.
- **AI work remains:** WebGPU supplies compute operations. A model loader,
  tensor engine, and a model's operator kernels and numerical tests must still
  be integrated. Embedded CPU execution is the experiment tracked in
  [CPU.md](CPU.md).

Successful startup is kept separate from successful compute, and software
compute is kept separate from hardware acceleration in the CI job names and
test output. Build once, then compare the same artifact's hash on each host
before making a broader portability claim.

## Direction: broad GPU coverage

The goal is to use as many GPU vendors and models as practical from one
application. Compatibility needs evidence for each OS, CPU architecture,
GPU/driver combination, and required compute feature. An API name alone does
not establish that a model's kernels will run correctly on a particular device.

The current Vulkan path is the first target for hardware tests on x86-64
Windows and Linux. The next application milestones are adapter enumeration and
selection, feature/limit reporting, and tests of more AI operators. The new
`--matmul` check provides an initial tiled f32 matrix operation; its embedded
execution is tracked in [CPU.md](CPU.md). Keep an implementation with modest feature
requirements, then select optimized kernels only when the device provides
their required features. A portable CPU implementation is also needed when
no suitable GPU path is available; the external lavapipe driver used in CI is
not an embedded CPU fallback.

[Upstream wgpu](https://github.com/gfx-rs/wgpu#supported-platforms) also has
Metal and Direct3D 12 backends. Adding them to this APE requires their native
library, ABI, runtime, and architecture integration; enabling Cargo features
alone does not provide it. CUDA/HIP/OpenCL-specific host runtimes and GPU
libraries likewise need explicit integration if a workload depends on them.

[HipScript](https://github.com/lights0123/hipscript/) is a useful reference for
translating a restricted set of HIP/CUDA kernels to WebGPU. Evaluate its kernel
translation independently from its browser/WebAssembly host runtime. This
branch does not yet import or test HipScript. Translating fixed kernels during
the build and embedding their WGSL can keep additional compiler dependencies
off the deployment machine.

Using several GPUs simultaneously is another application milestone. It needs
per-device buffers and queues, an explicit division of work, and transfers and
synchronization between devices. This prototype does not pool GPU memory or
distribute a model across adapters.

## Sources and licenses

See [README.cosmo](README.cosmo) and [IMPORT.json](IMPORT.json) for immutable
source revisions, original file hashes, pruning, licenses, and local changes.
The port's small C/Python/shell helpers use the MIT license; the adapted compute
example keeps upstream's MIT OR Apache-2.0 choice. Original dependency notices
are retained and packaged with the build.
