/* Host-side harness for the production extractor. Use the host zlib ABI;
 * this tests archive behavior, not Cosmopolitan or cross-platform execution. */
#include <zlib.h>
#define COSMOPOLITAN_THIRD_PARTY_ZLIB_ZLIB_H_
#include "toolchain/native_zip.c"

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  return extract_sdk_zip(argv[1], argv[2]) ? 1 : 0;
}
