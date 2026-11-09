#ifndef TOOLCHAIN_LANGUAGE_BUILD_H_
#define TOOLCHAIN_LANGUAGE_BUILD_H_

/* Emit intermediates only; ownership, final linking, and publication belong
 * to the caller. Rust retains the no_std rust_main() C-ABI entry contract. */
int rust_compile_objects(const char *self, const char *source,
                         const char *directory, int multiarch);
int machin_compile_source(const char *self, const char *source,
                          const char *output);

#endif
