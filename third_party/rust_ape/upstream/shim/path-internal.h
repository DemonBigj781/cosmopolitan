#ifndef RUST_APE_PATH_INTERNAL_H_
#define RUST_APE_PATH_INTERNAL_H_
#include <stddef.h>
#include <libc/limits.h>
int __ape_shim_mkntpath2(const char *, char16_t[static PATH_MAX]);
size_t __ape_shim_normntpath(char16_t *, size_t);
char __ape_shim_cosmos_drive(void);
#endif
