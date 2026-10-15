# Unified Cosmopolitan APE toolchain

**Target:** one final APE executable incorporating components authored in C, C++, Rust and Machin. Transpilation is optional, not mandatory.

## Bootstrap milestone

The current `ape_toolchain.c` is a minimal single-binary command dispatcher, **not** a working Machin, sccache, xshell or WebGPU integration. Unsupported commands explicitly fail.

Build with a Cosmopolitan C compiler:

```sh
COSMOCC=/path/to/cosmocc sh toolchain/build.sh
```

Output: `o/toolchain/toolchain.com`.

## Next stages

1. Audit existing `rust-ape` and `wgpu-native` integration on the parent branch to reuse Rust object linking.
2. Pin and inspect Machin source, build dependencies and generated C.
3. Compile a Machin-generated C program into the same APE, then integrate compiler objects.
4. Port xshell and sccache **as source-preserving Rust components** through the existing Rust APE linker route where possible.
5. Implement real command dispatch and link all compatible objects in one final stage.
6. Test Windows and Linux and extend portability coverage after platform-specific APIs are audited.

Do not claim source components are integrated until their tests run in the combined APE.
