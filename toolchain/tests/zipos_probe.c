/* Exercise ZIPOS against the real SDK archive without extracting any files. */
#include <cosmo.h>
#include <stdio.h>
#include <string.h>

__static_yoink("zipos");

static int check_prefix(const char *path, const char *expected) {
  char buffer[128];
  size_t length = strlen(expected);
  FILE *file = fopen(path, "rb");
  if (!file) {
    perror(path);
    return 1;
  }
  size_t got = fread(buffer, 1, length, file);
  int failed = ferror(file) || got != length || memcmp(buffer, expected, length);
  if (fclose(file)) failed = 1;
  if (failed) {
    fprintf(stderr, "Unexpected embedded resource contents: %s\n", path);
    return 1;
  }
  printf("PASS: native ZIP read %s\n", path);
  return 0;
}

int main(void) {
  if (check_prefix("/zip/rust-ape/EMBEDDED.txt", "Rust-APE source snapshot;"))
    return 1;
  if (check_prefix("/zip/rust-ape-sdk/generated/x86_64-unknown-linux-musl.json", "{"))
    return 1;
  return 0;
}
