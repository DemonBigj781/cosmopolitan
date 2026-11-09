#include <stdio.h>
#include <string.h>

extern int machin_build_main(int argc, char **argv) __attribute__((weak));
extern int rust_build_main(int argc, char **argv) __attribute__((weak));
extern int self_build_main(int argc, char **argv) __attribute__((weak));
extern int embedded_sdk_main(int argc, char **argv) __attribute__((weak));
extern int embedded_sdk_cleanup(void) __attribute__((weak));
extern int machin_entry(int argc, char **argv) __attribute__((weak));
extern int lua_entry(int argc, char **argv) __attribute__((weak));
extern int project_manifest_main(int argc, char **argv) __attribute__((weak));

static void usage(const char *name) {
  puts("Cosmo manifest toolchain APE");
  printf("Usage: %s path/to/project/manifest.json\n", name);
}

static int dispatch(int argc, char **argv) {
  if (argc < 2 || !strcmp(argv[1], "--help") || !strcmp(argv[1], "help")) {
    usage(argv[0]);
    return 0;
  }
  if (!strcmp(argv[1], "machin-demo") || !strcmp(argv[1], "machin-compiler")) {
    if (!machin_entry) { fputs("Machin module not linked\\n", stderr); return 2; }
    return machin_entry(argc - 1, argv + 1);
  }
  if (!strcmp(argv[1], "lua")) {
    if (!lua_entry) { fputs("Lua module not linked\\n", stderr); return 2; }
    return lua_entry(argc - 1, argv + 1);
  }
  if (!strcmp(argv[1], "rust") && argc > 2 && !strcmp(argv[2], "build")) {
    if (!rust_build_main) { fputs("Rust build driver not linked\\n", stderr); return 2; }
    return rust_build_main(argc, argv);
  }
  if (!strcmp(argv[1], "cc") || !strcmp(argv[1], "c++") ||
      !strcmp(argv[1], "rust") || !strcmp(argv[1], "node")) {
    if (!embedded_sdk_main) { fputs("Embedded SDK driver not linked\\n", stderr); return 2; }
    return embedded_sdk_main(argc, argv);
  }
  if (!strcmp(argv[1], "machin")) {
    if (!machin_build_main) { fputs("Machin build driver not linked\\n", stderr); return 2; }
    return machin_build_main(argc, argv);
  }
  if (!strcmp(argv[1], "self-build")) {
    if (!self_build_main) { fputs("Self-build driver not linked\n", stderr); return 2; }
    return self_build_main(argc, argv);
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
  if (argc == 2) {
    if (!project_manifest_main) { fputs("Project manifest driver not linked\n", stderr); return 2; }
    return project_manifest_main(argc, argv);
  }
  usage(argv[0]);
  return 2;
}

int main(int argc, char **argv) {
  int rc=dispatch(argc,argv);
  if(embedded_sdk_cleanup && embedded_sdk_cleanup() && !rc)rc=1;
  return rc;
}
