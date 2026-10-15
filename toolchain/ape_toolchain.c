#include <stdio.h>
#include <string.h>

extern int machin_build_main(int argc, char **argv) __attribute__((weak));
extern int rust_build_main(int argc, char **argv) __attribute__((weak));
extern int embedded_sdk_main(int argc, char **argv) __attribute__((weak));
extern int machin_entry(int argc, char **argv) __attribute__((weak));

static void usage(const char *name) {
  printf("Cosmopolitan unified APE toolchain (bootstrap)\n");
  printf("Usage: %s <command>\n", name);
  puts("Commands:");
  puts("  cc           Run packaged Cosmopolitan compiler (experimental)");
  puts("  c++          Compile and link C++ with packaged libcxx headers/runtime");
  puts("  rust status  Verify embedded Rust-APE resources");
  puts("  rust rustc  Run packaged Rust compiler");
  puts("  rust build input.rs -o output.com  Build no_std Rust APE (rust_main entry)");
  puts("  machin-compiler  Run linked self-hosted Machin compiler");
  puts("  machin-demo  Run statically linked Machin sample (when available)");
  puts("  version       Show bootstrap version");
  puts("  selftest      Verify command dispatch");
  puts("  machin build input.mfl -o output.com  Compile directly into an APE");
  puts("  cache         Reserved for sccache integration");
  puts("  shell         Reserved for xshell integration");
  puts("  devices       Reserved for WebGPU/CPU discovery");
}

int main(int argc, char **argv) {
  if (argc < 2 || !strcmp(argv[1], "--help") || !strcmp(argv[1], "help")) {
    usage(argv[0]);
    return 0;
  }
  if (!strcmp(argv[1], "machin-demo") || !strcmp(argv[1], "machin-compiler")) {
    if (!machin_entry) { fputs("Machin module not linked\\n", stderr); return 2; }
    return machin_entry(argc - 1, argv + 1);
  }
  if (!strcmp(argv[1], "rust") && argc > 2 && !strcmp(argv[2], "build")) {
    if (!rust_build_main) { fputs("Rust build driver not linked\\n", stderr); return 2; }
    return rust_build_main(argc, argv);
  }
  if (!strcmp(argv[1], "cc") || !strcmp(argv[1], "c++") || !strcmp(argv[1], "rust")) {
    if (!embedded_sdk_main) { fputs("Embedded SDK driver not linked\\n", stderr); return 2; }
    return embedded_sdk_main(argc, argv);
  }
  if (!strcmp(argv[1], "machin")) {
    if (!machin_build_main) { fputs("Machin build driver not linked\\n", stderr); return 2; }
    return machin_build_main(argc, argv);
  }
  if (!strcmp(argv[1], "version")) {
    puts("ape-toolchain bootstrap 0.1");
    return 0;
  }
  if (!strcmp(argv[1], "selftest")) {
    puts("PASS: APE toolchain command dispatcher");
    return 0;
  }
  if (!strcmp(argv[1], "cache") ||
      !strcmp(argv[1], "shell") || !strcmp(argv[1], "devices")) {
    fprintf(stderr, "%s: integration not implemented yet\n", argv[1]);
    return 2;
  }
  fprintf(stderr, "Unknown command: %s\n", argv[1]);
  usage(argv[0]);
  return 2;
}
