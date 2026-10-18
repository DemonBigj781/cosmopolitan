/* Host-side harness for the production extractor. Use the host zlib ABI;
 * this tests archive behavior, not Cosmopolitan or cross-platform execution. */
#include <zlib.h>
#define COSMOPOLITAN_THIRD_PARTY_ZLIB_ZLIB_H_
#include "toolchain/native_zip.c"

int main(int argc, char **argv) {
  if (argc < 3 || argc > 5) return 2;
  enum SdkComponent component = SDK_ALL;
  if (argc >= 4) {
    if (!strcmp(argv[3], "c")) component = SDK_C;
    else if (!strcmp(argv[3], "rust")) component = SDK_RUST;
    else if (!strcmp(argv[3], "ape")) component = SDK_RUST_APE;
    else return 2;
  }
  if ((component == SDK_RUST) != (argc == 5)) return 2;
  return extract_sdk_zip(argv[1], argv[2], component, argc == 5 ? argv[4] : NULL) ? 1 : 0;
}
