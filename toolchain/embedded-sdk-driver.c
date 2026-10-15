/* Experimental SDK launcher. Uses native C ZIP extraction; no Python or unzip required.
 * Not yet a dependency-free or in-process compiler. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <dirent.h>
#include <limits.h>

extern int extract_sdk_zip(const char *archive,const char *root);

static int find_cc(const char *dir, char *out, size_t cap, int depth) {
  if (depth > 12) return 0;
  DIR *d = opendir(dir);
  if (!d) return 0;
  struct dirent *ent;
  while ((ent = readdir(d))) {
    if (!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) continue;
    char path[4096];
    if (snprintf(path,sizeof(path),"%s/%s",dir,ent->d_name)>=(int)sizeof(path)) continue;
    struct stat st;
    if (lstat(path,&st)) continue;
    if (!strcmp(ent->d_name,"cosmocc") && S_ISREG(st.st_mode)) {
      if (strlen(path)+1 <= cap) { strcpy(out,path); closedir(d); return 1; }
    }
    if (S_ISDIR(st.st_mode) && find_cc(path,out,cap,depth+1)) {
      closedir(d); return 1;
    }
  }
  closedir(d);
  return 0;
}
static int run(char *const argv[]) {
  pid_t p=fork();
  if(p<0) return 125;
  if(!p) { execvp(argv[0],argv); perror(argv[0]); _exit(127); }
  int status;
  if(waitpid(p,&status,0)<0) return 125;
  return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
static unsigned long long read64(const unsigned char *p) {
  unsigned long long x=0;
  for(int i=7;i>=0;i--) x=(x<<8)|p[i];
  return x;
}
static int split_payload(const char *self,const char *dest) {
  FILE *in=fopen(self,"rb");
  if(!in) { perror("open APE"); return 1; }
  if(fseek(in,0,SEEK_END)) { fclose(in); return 1; }
  long total=ftell(in);
  if(total<24 || fseek(in,total-24,SEEK_SET)) { fclose(in); return 1; }
  unsigned char footer[24];
  if(fread(footer,1,24,in)!=24 || memcmp(footer,"COSZIP01",8)) {
    fputs("Embedded ZIP footer missing\\n",stderr); fclose(in); return 1;
  }
  unsigned long long offset=read64(footer+8),length=read64(footer+16);
  if(offset>(unsigned long long)total-24 ||
     length>(unsigned long long)total-24-offset ||
     offset+length!=(unsigned long long)total-24 ||
     fseek(in,(long)offset,SEEK_SET)) {
    fputs("Invalid embedded ZIP boundaries\\n",stderr); fclose(in); return 1;
  }
  FILE *out=fopen(dest,"wb");
  if(!out) { perror("create ZIP"); fclose(in); return 1; }
  char buffer[65536];
  while(length) {
    size_t n=length<sizeof(buffer)?(size_t)length:sizeof(buffer);
    if(fread(buffer,1,n,in)!=n || fwrite(buffer,1,n,out)!=n) {
      fclose(in); fclose(out); return 1;
    }
    length-=n;
  }
  int error=fclose(out);
  fclose(in);
  return error!=0;
}
int embedded_sdk_main(int argc,char **argv) {
  if(argc<2) { fputs("Usage: toolchain.com cc [cosmocc arguments]\n",stderr); return 2; }
  char self[4096],tmp[]="/tmp/cosmo-sdk-XXXXXX",compiler[4096];
  if(!realpath(argv[0],self)) { perror("resolve executable"); return 1; }
  if(!mkdtemp(tmp)) { perror("mkdtemp"); return 1; }
  /* Transitional Python-free extraction using host unzip.
   * Next step: replace with Cosmopolitan's in-process ZIP reader. */
  char archive[4096];
  if(snprintf(archive,sizeof(archive),"%s/sdk.zip",tmp)>=(int)sizeof(archive)) return 1;
  if(split_payload(self,archive)) return 1;
  int rc=extract_sdk_zip(archive,tmp);
  if(rc) { fprintf(stderr,"SDK extraction failed: %d\n",rc); return rc; }
  if (!strcmp(argv[1], "rust")) {
    if (argc < 3 || (strcmp(argv[2], "status") && strcmp(argv[2], "rustc") && strcmp(argv[2], "cargo"))) {
      fputs("Usage: toolchain.com rust status | rust rustc [args] | rust cargo [args]\\n",stderr);
      return 2;
    }
    char script[4096], marker[4096];
    if (snprintf(script,sizeof(script),"%s/rust-ape/run.sh",tmp)>=(int)sizeof(script) ||
        snprintf(marker,sizeof(marker),"%s/rust-ape/EMBEDDED.txt",tmp)>=(int)sizeof(marker))
      return 1;
    if (access(script,R_OK) || access(marker,R_OK)) {
      fputs("Rust-APE source snapshot missing from embedded SDK\\n",stderr);
      return 1;
    }
    char rustc[4096],cargo[4096],sysroot[4096],target[4096],apelink[4096];
    if (snprintf(rustc,sizeof(rustc),"%s/rust-toolchain/bin/rustc",tmp)>=(int)sizeof(rustc) ||
        snprintf(sysroot,sizeof(sysroot),"%s/rust-toolchain",tmp)>=(int)sizeof(sysroot) ||
        snprintf(cargo,sizeof(cargo),"%s/rust-toolchain/bin/cargo",tmp)>=(int)sizeof(cargo) ||
        snprintf(target,sizeof(target),"%s/rust-ape-sdk/generated/x86_64-unknown-linux-musl.json",tmp)>=(int)sizeof(target) ||
        snprintf(apelink,sizeof(apelink),"%s/rust-ape-sdk/vendor/cosmocc/bin/apelink",tmp)>=(int)sizeof(apelink))
      return 1;
    if (!strcmp(argv[2], "status")) {
      puts("PASS: embedded Rust-APE source snapshot extracted");
      puts(access(rustc,X_OK) ? "Rust compiler: not packaged" : "Rust compiler: packaged");
      puts(access(cargo,X_OK) ? "Cargo: not packaged" : "Cargo: packaged");
      puts(access(cosmo_target,R_OK) ? "Rust-APE patched target: not packaged" : "Rust-APE patched target: packaged");
      puts(access(target,R_OK) || access(apelink,X_OK) ? "Patched Rust-APE SDK: not packaged" : "Patched Rust-APE SDK: packaged");
      return 0;
    }
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
  if(!find_cc(tmp,compiler,sizeof(compiler),0)) {
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
  /* Temporary SDK directory retained for diagnostics and compiler subprocesses. */
  fprintf(stderr,"SDK extracted at %s (temporary bootstrap mode)\n",tmp);
  return rc;
}
