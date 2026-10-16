#include "toolchain/tempdir.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int main(int argc, char **argv) {
  struct ToolchainTemp dir = TOOLCHAIN_TEMP_INIT;
  if (argc != 3 || toolchain_temp_open(&dir, "owned-test")) return 2;
  printf("%s\n", dir.path);
  fflush(stdout);
  if (mkdirat(dir.fd, "nested", 0700)) return 3;
  int fd = openat(dir.fd, "nested/data", O_CREAT | O_WRONLY, 0600);
  if (fd < 0 || write(fd, "test", 4) != 4 || close(fd)) return 3;
  if (symlinkat(argv[2], dir.fd, "outside-link")) return 3;
  if (!strcmp(argv[1], "replace")) {
    char moved[4096];
    if (snprintf(moved, sizeof(moved), "%s-moved", dir.path) >= sizeof(moved)) return 3;
    if (rename(dir.path, moved) || mkdir(dir.path, 0700)) return 3;
    char marker[4096];
    if (snprintf(marker, sizeof(marker), "%s/keep", dir.path) >= sizeof(marker)) return 3;
    fd = open(marker, O_CREAT | O_WRONLY, 0600);
    if (fd < 0 || close(fd)) return 3;
    int rc = toolchain_temp_close(&dir);
    return rc && errno == ESTALE ? 0 : 4;
  }
  int rc = toolchain_temp_close(&dir);
  if (rc) perror("cleanup owned test directory");
  return rc ? 1 : 0;
}
