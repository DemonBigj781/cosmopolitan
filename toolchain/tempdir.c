/* Cleanup is restricted to a directory created and held open by this process.
 * Descendant symlinks are unlinked, never followed. */
#include "tempdir.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#ifdef __COSMOPOLITAN__
#include <cosmo.h>
#endif

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

static int windows_image(const char *name, mode_t mode) {
  if (!S_ISREG(mode)) return 0;
#ifdef __COSMOPOLITAN__
  if (!IsWindows()) return 0;
#elif !defined(_WIN32)
  return 0;
#endif
  size_t length = strlen(name);
  return length >= 4 && (!strcasecmp(name + length - 4, ".exe") ||
                         !strcasecmp(name + length - 4, ".dll"));
}

static int unlink_owned_file(int fd, const char *name, const char *path,
                             mode_t mode, int *image_wait_budget) {
  int waited = 0;
  for (;;) {
    if (!unlinkat(fd, name, 0)) {
      if (waited) fprintf(stderr, "Released owned Windows image after %d waits: %s\n", waited, path);
      return 0;
    }
    int error = errno;
    /* Native image handles can briefly outlive a completed Windows process.
     * Only owned regular image files qualify; never change ACLs or permissions.
     * The finite budget is shared by the entire tree, not reset per file. */
    if (!windows_image(name, mode) || (error != EACCES && error != EBUSY) || !*image_wait_budget) {
      errno = error; return -1;
    }
    --*image_wait_budget;
    if (!waited++) fprintf(stderr, "Waiting for owned Windows image release: %s\n", path);
    struct timespec delay = {0, 50000000};
    while (nanosleep(&delay, &delay)) {
      if (errno != EINTR) { errno = error; return -1; }
    }
  }
}

static int remove_contents(int fd, const char *path, int *image_wait_budget) {
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
    char child_path[4096];
    if (snprintf(child_path, sizeof(child_path), "%s/%s", path, entry->d_name) >= (int)sizeof(child_path)) {
      error = ENAMETOOLONG; break;
    }
    struct stat st;
    if (fstatat(fd, entry->d_name, &st, AT_SYMLINK_NOFOLLOW)) {
      error = errno; fprintf(stderr, "inspect owned entry %s: %s\n", child_path, strerror(error)); break;
    }
    if (S_ISDIR(st.st_mode)) {
      int child = openat(fd, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      if (child == -1) {
        error = errno; fprintf(stderr, "open owned directory %s: %s\n", child_path, strerror(error)); break;
      }
      int rc = remove_contents(child, child_path, image_wait_budget);
      if (rc) error = errno;
      if (close(child) && !error) error = errno;
      if (error) break;
      if (unlinkat(fd, entry->d_name, AT_REMOVEDIR)) {
        error = errno; fprintf(stderr, "remove owned directory %s: %s\n", child_path, strerror(error)); break;
      }
    } else if (unlink_owned_file(fd, entry->d_name, child_path, st.st_mode, image_wait_budget)) {
      error = errno; fprintf(stderr, "remove owned entry %s: %s\n", child_path, strerror(error)); break;
    }
  }
  if (closedir(directory) && !error) error = errno;
  if (error) { errno = error; return -1; }
  return 0;
}

int toolchain_temp_close(struct ToolchainTemp *dir) {
  if (dir->fd == -1) return 0;
  struct stat owned, current;
  int error = 0, image_wait_budget = 100;
  if (fstat(dir->fd, &owned) || lstat(dir->path, &current)) error = errno;
  else if (!S_ISDIR(current.st_mode) || owned.st_dev != current.st_dev ||
           owned.st_ino != current.st_ino) error = ESTALE;
  if (!error && remove_contents(dir->fd, dir->path, &image_wait_budget)) error = errno;
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
