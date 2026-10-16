#ifndef TOOLCHAIN_TEMPDIR_H_
#define TOOLCHAIN_TEMPDIR_H_

struct ToolchainTemp {
  char path[4096];
  int fd;
};

#define TOOLCHAIN_TEMP_INIT { "", -1 }

int toolchain_temp_open(struct ToolchainTemp *dir, const char *prefix);
int toolchain_temp_close(struct ToolchainTemp *dir);

#endif
