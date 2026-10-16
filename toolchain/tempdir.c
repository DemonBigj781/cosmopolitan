/* Cleanup is restricted to a directory created and held open by this process.
 * Descendant symlinks are unlinked, never followed. */
#include "tempdir.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int toolchain_temp_open(struct ToolchainTemp *dir, const char *prefix) {
  char base[4096];
  const char *tmp = getenv("TMPDIR");
  if (dir->fd != -1) { errno = EBUSY; return -1; }
  if (!realpath(tmp && *tmp ? tmp : "/tmp", base)) return -1;
  if (snprintf(dir->path, sizeof(dir->path), "%s/%s-XXXXXX", base, prefix) >=
      (int)sizeof(dir->path)) { errno = ENAMETOOLONG; return -1; }
  if (!mkdtemp(dir->path)) return -1;
  dir->fd = open(dir->path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (dir->fd == -1) {
    int error = errno;
    rmdir(dir->path);
    errno = error;
    return -1;
  }
  return 0;
}

static int remove_contents(int fd) {
  /* Use a fresh directory description, not a dup of an older directory view
   * or a descriptor whose iteration offset another operation has advanced. */
  int copy = openat(fd, ".", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (copy == -1) return -1;
  DIR *directory = fdopendir(copy);
  if (!directory) { int error = errno; close(copy); errno = error; return -1; }
  int error = 0;
  for (;;) {
    errno = 0;
    struct dirent *entry = readdir(directory);
    if (!entry) { if (errno) error = errno; break; }
    if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
    struct stat st;
    if (fstatat(fd, entry->d_name, &st, AT_SYMLINK_NOFOLLOW)) { error = errno; break; }
    if (S_ISDIR(st.st_mode)) {
      int child = openat(fd, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      if (child == -1) { error = errno; break; }
      int rc = remove_contents(child);
      if (rc) error = errno;
      if (close(child) && !error) error = errno;
      if (error) break;
      if (unlinkat(fd, entry->d_name, AT_REMOVEDIR)) { error = errno; break; }
    } else if (unlinkat(fd, entry->d_name, 0)) { error = errno; break; }
  }
  if (closedir(directory) && !error) error = errno;
  if (error) { errno = error; return -1; }
  return 0;
}

int toolchain_temp_close(struct ToolchainTemp *dir) {
  if (dir->fd == -1) return 0;
  struct stat owned, current;
  int error = 0;
  if (fstat(dir->fd, &owned) || lstat(dir->path, &current)) error = errno;
  else if (!S_ISDIR(current.st_mode) || owned.st_dev != current.st_dev ||
           owned.st_ino != current.st_ino) error = ESTALE;
  if (!error && remove_contents(dir->fd)) error = errno;
  if (close(dir->fd) && !error) error = errno;
  dir->fd = -1;
  if (!error) {
    if (lstat(dir->path, &current)) error = errno;
    else if (!S_ISDIR(current.st_mode) || owned.st_dev != current.st_dev ||
             owned.st_ino != current.st_ino) error = ESTALE;
    else if (rmdir(dir->path)) error = errno;
  }
  if (error) { errno = error; return -1; }
  return 0;
}
