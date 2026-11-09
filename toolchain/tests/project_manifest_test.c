#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "toolchain/sdk_resources.h"
#include "toolchain/language_build.h"

int project_manifest_main(int, char **);

static const char *g_sdk_root;

int embedded_sdk_materialize(const char *self, enum SdkComponent component) {
  (void)self;
  (void)component;
  g_sdk_root = getenv("TEST_SDK_ROOT");
  if (!g_sdk_root) {
    fputs("TEST_SDK_ROOT is required\n", stderr);
    return 1;
  }
  return 0;
}

const char *embedded_sdk_root(void) {
  return g_sdk_root;
}

static int log_arguments(int argc, char **argv) {
  const char *path = getenv("TEST_ARGUMENT_LOG");
  if (!path) return 0;
  FILE *file = fopen(path, "w");
  if (!file) return -1;
  for (int i = 0; i < argc; ++i) {
    if (fprintf(file, "%s\n", argv[i]) < 0) {
      fclose(file);
      return -1;
    }
  }
  return fclose(file);
}

static const char *output_argument(int argc, char **argv) {
  for (int i = 0; i + 1 < argc; ++i) {
    if (!strcmp(argv[i], "-o")) return argv[i + 1];
  }
  return NULL;
}

static int fake_compile(int argc, char **argv) {
  if (log_arguments(argc, argv)) {
    perror("write compiler argument log");
    return 1;
  }
  const char *output = output_argument(argc, argv);
  if (!output) {
    fputs("fake compiler did not receive -o\n", stderr);
    return 2;
  }
  FILE *file = fopen(output, "w");
  if (!file) {
    perror(output);
    return 1;
  }
  int failed = 0;
  if (getenv("TEST_PE_OUTPUT")) {
    unsigned char image[192] = {0};
    image[0] = 'M'; image[1] = 'Z'; image[60] = 64;
    memcpy(image + 64, "PE\0\0", 4);
    image[68] = 0x64; image[69] = 0x86;
    image[84] = 96; image[88] = 0x0b; image[89] = 0x02;
    image[162] = 1; /* 64 KiB initial stack reserve. */
    image[169] = 0x10; /* 4 KiB initial stack commit. */
    failed = fwrite(image, 1, sizeof(image), file) != sizeof(image);
  } else {
    failed = fputs("student output\n", file) == EOF;
  }
  if (fclose(file)) failed = 1;
  if (failed) {
    perror(output);
    return 1;
  }
  if (getenv("TEST_COMPILER_SIDECARS")) {
    char sidecar[4096];
    size_t length = strlen(output);
    if (length < 4 || strcmp(output + length - 4, ".com")) return 1;
    const char *suffixes[] = {".com.dbg", ".aarch64.elf"};
    for (size_t i = 0; i < 2; ++i) {
      if (snprintf(sidecar, sizeof(sidecar), "%.*s%s", (int)(length - 4),
                   output, suffixes[i]) >= (int)sizeof(sidecar)) return 1;
      FILE *debug = fopen(sidecar, "w");
      if (!debug || fclose(debug)) return 1;
    }
  }
  const char *failure = getenv("TEST_COMPILER_FAIL");
  return failure ? atoi(failure) : 0;
}

int embedded_sdk_main(int argc, char **argv) {
  return fake_compile(argc, argv);
}

static int copy_file(const char *source, const char *output) {
  FILE *input = fopen(source, "rb");
  if (!input) return -1;
  FILE *target = fopen(output, "wb");
  if (!target) {
    fclose(input);
    return -1;
  }
  char buffer[4096];
  size_t count;
  int rc = 0;
  while ((count = fread(buffer, 1, sizeof(buffer), input))) {
    if (fwrite(buffer, 1, count, target) != count) {
      rc = -1;
      break;
    }
  }
  if (ferror(input)) rc = -1;
  if (fclose(input)) rc = -1;
  if (fclose(target)) rc = -1;
  return rc;
}

int embedded_sdk_run_script(char *const argv[]) {
  int argc = 0;
  while (argv[argc]) ++argc;
  if (strstr(argv[0], "x86_64-unknown-cosmo-cc") ||
      strstr(argv[0], "x86_64-unknown-cosmo-c++"))
    return fake_compile(argc, (char **)argv);
  const char *output = output_argument(argc, (char **)argv);
  if (output && argc > 0 && !copy_file(argv[argc - 1], output)) return 0;
  errno = ENOTSUP;
  return 1;
}

static int frontend_record(const char *name, const char *source,
                           const char *directory, int multiarch) {
  const char *path = getenv("TEST_FRONTEND_LOG");
  if (!path) return 1;
  FILE *file = fopen(path, "w");
  if (!file) return 1;
  int failed = fprintf(file, "%s\n%s\n%s\n%d\n", name, source,
                       directory, multiarch) < 0;
  if (fclose(file)) failed = 1;
  return failed;
}

static int write_fixture(const char *path) {
  FILE *file = fopen(path, "w");
  if (!file) return 1;
  int failed = fputs("int main(void) { return 0; }\n", file) == EOF;
  if (fclose(file)) failed = 1;
  return failed;
}

int rust_compile_objects(const char *self, const char *source,
                         const char *directory, int multiarch) {
  (void)self;
  if (frontend_record("rust", source, directory, multiarch)) return 1;
  char object[4096];
  if (snprintf(object, sizeof(object), "%s/rust.o", directory) >= (int)sizeof(object) ||
      write_fixture(object)) return 1;
  if (multiarch) {
    char arm[4096];
    if (snprintf(arm, sizeof(arm), "%s/.aarch64", directory) >= (int)sizeof(arm) ||
        mkdir(arm, 0700) ||
        snprintf(object, sizeof(object), "%s/rust.o", arm) >= (int)sizeof(object) ||
        write_fixture(object)) return 1;
  }
  const char *failure = getenv("TEST_FRONTEND_FAIL");
  return failure ? atoi(failure) : 0;
}

int machin_compile_source(const char *self, const char *source, const char *output) {
  (void)self;
  if (frontend_record("machin", source, output, 0) || write_fixture(output)) return 1;
  const char *failure = getenv("TEST_FRONTEND_FAIL");
  return failure ? atoi(failure) : 0;
}

int main(int argc, char **argv) {
  return project_manifest_main(argc, argv);
}
