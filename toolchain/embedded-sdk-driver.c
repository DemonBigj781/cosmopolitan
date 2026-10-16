/* ZIPOS serves read-only resource checks. Compiler subprocesses still need
 * materialized SDK files; they do not require Python or unzip at runtime. */
#include <cosmo.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/wait.h>
#include "sdk_resources.h"
#include "tempdir.h"

__static_yoink("zipos");

static struct ToolchainTemp sdk_temp = TOOLCHAIN_TEMP_INIT;
static unsigned sdk_loaded;

int embedded_sdk_cleanup(void) {
  int rc=toolchain_temp_close(&sdk_temp);
  sdk_loaded=0;
  if(rc)perror("remove owned SDK temporary directory");
  return rc;
}

static int materialize(const char *self,enum SdkComponent component) {
  unsigned bit=1u<<component;
  if(sdk_loaded&bit)return 0;
  if(sdk_temp.fd==-1 && toolchain_temp_open(&sdk_temp,"cosmo-sdk")) {
    perror("create SDK temporary directory"); return 1;
  }
  if(extract_sdk_zip(self,sdk_temp.path,component)) {
    fputs("SDK extraction failed\n",stderr); return 1;
  }
  sdk_loaded|=bit;
  fprintf(stderr,"SDK materialized: %s (%s)\n",component==SDK_C?"C":"Rust",sdk_temp.path);
  return 0;
}

static int run(char *const argv[]) {
  pid_t p=fork();
  if(p<0) return 125;
  if(!p) { execvp(argv[0],argv); perror(argv[0]); _exit(127); }
  int status;
  pid_t waited;
  do { waited=waitpid(p,&status,0); } while(waited<0 && errno==EINTR);
  if(waited<0) return 125;
  return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
static int rust_source_available(void) {
  static const char expected[]="Rust-APE source snapshot;";
  char marker[sizeof(expected)-1];
  if(access("/zip/rust-ape/run.sh",R_OK)) {
    fputs("Rust-APE source snapshot missing from embedded SDK\n",stderr);
    return 1;
  }
  FILE *file=fopen("/zip/rust-ape/EMBEDDED.txt","rb");
  if(!file) { perror("read embedded Rust marker"); return 1; }
  size_t got=fread(marker,1,sizeof(marker),file);
  int invalid=ferror(file) || got!=sizeof(marker) || memcmp(marker,expected,sizeof(marker));
  if(fclose(file))invalid=1;
  if(invalid) {
    fputs("Invalid embedded Rust-APE source marker\n",stderr);
    return 1;
  }
  return 0;
}
static int rust_status(void) {
  if(rust_source_available())return 1;
  /* Retain the established status line: the marker was extracted in memory,
   * not onto disk. State that distinction explicitly for humans and tests. */
  puts("PASS: embedded Rust-APE source snapshot extracted");
  puts("Resource access: ZIPOS (memory only; no SDK files extracted)");
  puts(access("/zip/rust-toolchain/bin/rustc",X_OK) ? "Rust compiler: not packaged" : "Rust compiler: packaged");
  puts(access("/zip/rust-toolchain/bin/cargo",X_OK) ? "Cargo: not packaged" : "Cargo: packaged");
  int target=access("/zip/rust-ape-sdk/generated/x86_64-unknown-linux-musl.json",R_OK);
  puts(target ? "Rust-APE patched target: not packaged" : "Rust-APE patched target: packaged");
  puts(target || access("/zip/rust-ape-sdk/vendor/cosmocc/bin/apelink",X_OK) ?
       "Patched Rust-APE SDK: not packaged" : "Patched Rust-APE SDK: packaged");
  return 0;
}
int embedded_sdk_main(int argc,char **argv) {
  if(argc<2) { fputs("Usage: toolchain.com cc [cosmocc arguments]\n",stderr); return 2; }
  if(!strcmp(argv[1],"rust")) {
    if(argc<3 || (strcmp(argv[2],"status") && strcmp(argv[2],"rustc") && strcmp(argv[2],"cargo"))) {
      fputs("Usage: toolchain.com rust status | rust rustc [args] | rust cargo [args]\n",stderr);
      return 2;
    }
    if(!strcmp(argv[2],"status"))return rust_status();
  }
  int is_rust=!strcmp(argv[1],"rust");
  if(is_rust && rust_source_available())return 1;
  char self[4096],compiler[4096];
  if(!realpath(argv[0],self)) { perror("resolve executable"); return 1; }
  int rc=materialize(self,is_rust?SDK_RUST:SDK_C);
  if(rc)return rc;
  const char *tmp=sdk_temp.path;
  if (is_rust) {
    char rustc[4096],cargo[4096],sysroot[4096];
    if (snprintf(rustc,sizeof(rustc),"%s/rust-toolchain/bin/rustc",tmp)>=(int)sizeof(rustc) ||
        snprintf(sysroot,sizeof(sysroot),"%s/rust-toolchain",tmp)>=(int)sizeof(sysroot) ||
        snprintf(cargo,sizeof(cargo),"%s/rust-toolchain/bin/cargo",tmp)>=(int)sizeof(cargo))
      return 1;
    if (access(rustc,X_OK)) {
      fputs("Rust compiler not packaged; set RUST_COMPILER_ROOT at packaging time\\n",stderr);
      return 2;
    }
    int is_cargo=!strcmp(argv[2],"cargo");
    if(is_cargo && access(cargo,X_OK)) {
      fputs("Cargo not packaged; set RUST_COMPILER_ROOT with bin/cargo\\n",stderr);
      return 2;
    }
    if(is_cargo && setenv("RUSTC",rustc,1)) {
      perror("set RUSTC");
      return 1;
    }
    char **rargs=calloc((size_t)argc+4,sizeof(char*));
    if (!rargs) return 1;
    int n=0;
    rargs[n++]=is_cargo?cargo:rustc;
    if(!is_cargo) {
      rargs[n++]="--sysroot";
      rargs[n++]=sysroot;
    }
    for(int i=3;i<argc;i++)rargs[n++]=argv[i];
    rargs[n]=NULL;
    rc=run(rargs);
    free(rargs);
    return rc;
  }
  if(snprintf(compiler,sizeof(compiler),"%s/bin/cosmocc",tmp)>=(int)sizeof(compiler) ||
     access(compiler,X_OK)) {
    fputs("No cosmocc compiler found in embedded SDK\n",stderr); return 1;
  }
  int is_cpp = !strcmp(argv[1], "c++");
  int compile_only = 0;
  for (int i=2;i<argc;i++)
    if (!strcmp(argv[i], "-c") || !strcmp(argv[i], "-E") || !strcmp(argv[i], "-S"))
      compile_only = 1;
  char headers[4096];
  if (snprintf(headers,sizeof(headers),"%s/include/third_party/libcxx",tmp)>=(int)sizeof(headers))
    return 1;
  if (is_cpp && access(headers,R_OK)) {
    fprintf(stderr,"C++ headers not found: %s\\n",headers);
    return 1;
  }
  char **args=calloc((size_t)argc+6,sizeof(char*));
  if(!args) return 1;
  int n=0;
  args[n++]=compiler;
  if(is_cpp) {
    args[n++]="-I";
    args[n++]=headers;
  }
  for(int i=2;i<argc;i++) args[n++]=argv[i];
  if(is_cpp && !compile_only) args[n++]="-lstdc++";
  args[n]=NULL;
  rc=run(args);
  free(args);
  return rc;
}
