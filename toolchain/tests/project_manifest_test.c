#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "toolchain/sdk_resources.h"

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
  if (fputs("student output\n", file) == EOF || fclose(file)) {
    perror(output);
    return 1;
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

int main(int argc, char **argv) {
  return project_manifest_main(argc, argv);
}
