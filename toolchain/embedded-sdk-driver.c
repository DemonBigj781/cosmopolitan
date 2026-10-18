/* ZIPOS serves read-only resource checks. Compiler subprocesses still need
 * materialized SDK files; they do not require Python or unzip at runtime. */
#include <cosmo.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/wait.h>
#include "sdk_resources.h"
#include "tempdir.h"

__static_yoink("zipos");

static struct ToolchainTemp sdk_temp = TOOLCHAIN_TEMP_INIT;
static unsigned sdk_loaded;

#define WINDOWS_SHELL_RESET \
  "unset _COSMO_FDS_V2 _COSMO_PID _COSMO_PPID _COSMO_STOP_CHURN " \
  "_COSMO_STOP_EVENT _SIGS _MASK _IGNS; "

static const char *rust_host_triple(void) {
  /* The Windows PE member is x64, including execution on Windows ARM. */
  if(IsWindows())return "x86_64-pc-windows-msvc";
#if defined(__aarch64__)
  if(IsLinux())return "aarch64-unknown-linux-gnu";
  if(IsXnu())return "aarch64-apple-darwin";
#else
  if(IsLinux())return "x86_64-unknown-linux-gnu";
  if(IsXnu())return "x86_64-apple-darwin";
#endif
  return NULL;
}

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
  if(extract_sdk_zip(self,sdk_temp.path,component,component==SDK_RUST?rust_host_triple():NULL)) {
    fputs("SDK extraction failed\n",stderr); return 1;
  }
  sdk_loaded|=bit;
  fprintf(stderr,"SDK materialized: %s (%s)\n",component==SDK_C?"C":"Rust",sdk_temp.path);
  return 0;
}

static int make_pipe(int fds[2]) {
  if(pipe(fds))return -1;
  if(fcntl(fds[0],F_SETFD,FD_CLOEXEC) || fcntl(fds[1],F_SETFD,FD_CLOEXEC)) {
    int error=errno;close(fds[0]);close(fds[1]);errno=error;return -1;
  }
  return 0;
}
static int forward_output(int out,int err) {
  struct pollfd fds[2]={{out,POLLIN,0},{err,POLLIN,0}};
  int open=2,rc=0;
  while(open) {
    int ready=poll(fds,2,-1);
    if(ready<0) {if(errno==EINTR)continue;rc=125;break;}
    for(int i=0;i<2;i++)if(fds[i].fd!=-1 && fds[i].revents) {
      char buffer[8192];
      ssize_t count=read(fds[i].fd,buffer,sizeof(buffer));
      if(count<0 && errno==EINTR)continue;
      if(count<=0) {
        if(count<0)rc=125;
        close(fds[i].fd);fds[i].fd=-1;open--;continue;
      }
      for(ssize_t done=0;done<count;) {
        ssize_t wrote=write(i+1,buffer+done,(size_t)(count-done));
        if(wrote<0 && errno==EINTR)continue;
        if(wrote<=0) {rc=125;break;}
        done+=wrote;
      }
    }
  }
  for(int i=0;i<2;i++)if(fds[i].fd!=-1)close(fds[i].fd);
  return rc;
}
static int run(char *const argv[]) {
  int out[2],err[2];
  int relay=IsWindows(),io_error=0;
  /* Native shells and APE grandchildren cannot safely share overlapped file
   * offsets. Pipes let this process own all writes to the caller's log files. */
  if(relay) {
    if(make_pipe(out))return 125;
    if(make_pipe(err)) {close(out[0]);close(out[1]);return 125;}
  }
  pid_t p=fork();
  if(p<0) {
    if(relay) {close(out[0]);close(out[1]);close(err[0]);close(err[1]);}
    return 125;
  }
  if(!p) {
    if(relay) {
      close(out[0]);close(err[0]);
      if(dup2(out[1],1)<0 || dup2(err[1],2)<0)_exit(125);
      close(out[1]);close(err[1]);
    }
    execvp(argv[0],argv);perror(argv[0]);_exit(127);
  }
  if(relay) {close(out[1]);close(err[1]);io_error=forward_output(out[0],err[0]);}
  int status;
  pid_t waited;
  do { waited=waitpid(p,&status,0); } while(waited<0 && errno==EINTR);
  if(waited<0) return 125;
  if(io_error)return io_error;
  return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
static int run_external(char *const argv[]) {
  if(!IsWindows())return run(argv);
  int count=0,rc=1;
  while(argv[count])count++;
  char **args=calloc((size_t)count+1,sizeof(char*));
  if(!args)return 1;
  for(int i=0;i<count;i++) {
    if(!(args[i]=strdup(argv[i])))goto done;
    char *p=args[i];
    if(strlen(p)<3)continue;
    /* Win32 spawning translates /C/... to C:\... . A shell's dirname
     * expansions need slashes, so pass C:/... through that boundary instead.
     * Only complete drive-rooted arguments are paths; compiler flags and
     * backslash-containing macro definitions must remain byte-for-byte intact. */
    if(p[0]=='/' && ((p[1]>='A' && p[1]<='Z') || (p[1]>='a' && p[1]<='z')) && p[2]=='/') {
      p[0]=p[1];p[1]=':';
    }
    if(((p[0]>='A' && p[0]<='Z') || (p[0]>='a' && p[0]<='z')) &&
       p[1]==':' && (p[2]=='/' || p[2]=='\\'))
      for(char *q=p;*q;q++)if(*q=='\\')*q='/';
  }
  rc=run(args);
done:
  for(int i=0;i<count;i++)free(args[i]);
  free(args);
  return rc;
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
static int rust_paths(const char *root,char *sysroot,char *rustc,char *cargo) {
  const char *host=rust_host_triple(),*suffix=IsWindows()?".exe":"";
  if(!host)return -1;
  return snprintf(sysroot,4096,"%s/rust-toolchain/hosts/%s",root,host)>=4096 ||
         snprintf(rustc,4096,"%s/bin/rustc%s",sysroot,suffix)>=4096 ||
         snprintf(cargo,4096,"%s/bin/cargo%s",sysroot,suffix)>=4096 ? -1 : 0;
}
static uint32_t read32le(const unsigned char *p) {
  return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
/* Host selection is explicit. Reject a mislabeled binary before extraction. */
static int rust_binary_matches(const char *path) {
  unsigned char header[64];
  FILE *file=fopen(path,"rb");
  if(!file)return 0;
  size_t got=fread(header,1,sizeof(header),file);
  int match=0;
#if defined(__aarch64__)
  int elf_machine=183;uint32_t macho_machine=0x0100000c;
#else
  int elf_machine=62;uint32_t macho_machine=0x01000007;
#endif
  if(got==sizeof(header) && !ferror(file)) {
    if(IsLinux() && !memcmp(header,"\177ELF\2\1",6))
      match=header[18]==elf_machine && header[19]==0;
    else if(IsXnu() && read32le(header)==0xfeedfacf)
      match=read32le(header+4)==macho_machine;
    else if(IsWindows() && !memcmp(header,"MZ",2)) {
      unsigned char pe[6];
      match=!fseek(file,read32le(header+0x3c),SEEK_SET) && fread(pe,1,sizeof(pe),file)==sizeof(pe) &&
            !memcmp(pe,"PE\0\0\x64\x86",6);
    }
  }
  if(fclose(file))match=0;
  return match;
}
static int rust_host_available(void) {
  const char *host=rust_host_triple();
  char sysroot[4096],rustc[4096],cargo[4096];
  if(rust_paths("/zip",sysroot,rustc,cargo) || access(rustc,R_OK) || access(cargo,R_OK)) {
    fprintf(stderr,"Rust compiler not packaged for %s; no SDK extracted\n",host?host:"this platform");
    return 2;
  }
  if(!rust_binary_matches(rustc) || !rust_binary_matches(cargo)) {
    fprintf(stderr,"Packaged Rust compiler does not match %s; no SDK extracted\n",host);
    return 126;
  }
  return 0;
}
static int rust_status(void) {
  if(rust_source_available())return 1;
  /* Retain the established status line: the marker was extracted in memory,
   * not onto disk. State that distinction explicitly for humans and tests. */
  puts("PASS: embedded Rust-APE source snapshot extracted");
  puts("Resource access: ZIPOS (memory only; no SDK files extracted)");
  char sysroot[4096],rustc[4096],cargo[4096];
  int unavailable=rust_paths("/zip",sysroot,rustc,cargo);
  puts(unavailable || access(rustc,X_OK) ? "Rust compiler: not packaged" : "Rust compiler: packaged");
  puts(unavailable || access(cargo,X_OK) ? "Cargo: not packaged" : "Cargo: packaged");
  const char *host=rust_host_triple();
  printf("Rust compiler platform: %s\n",host?host:"unsupported platform");
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
  int rc;
  if(is_rust && (rc=rust_host_available()))return rc;
  char self[4096],compiler[4096];
  if(!realpath(argv[0],self)) { perror("resolve executable"); return 1; }
  rc=materialize(self,is_rust?SDK_RUST:SDK_C);
  if(rc)return rc;
  const char *tmp=sdk_temp.path;
  if (is_rust) {
    char rustc[4096],cargo[4096],sysroot[4096];
    if(rust_paths(tmp,sysroot,rustc,cargo))return 1;
    if (access(rustc,X_OK)) {
      fputs("Rust compiler not packaged; set RUST_COMPILER_ROOT at packaging time\\n",stderr);
      return 2;
    }
    int is_cargo=!strcmp(argv[2],"cargo");
    if(is_cargo && access(cargo,X_OK)) {
      fputs("Cargo not packaged; set RUST_COMPILER_ROOT with bin/cargo\\n",stderr);
      return 2;
    }
    if(is_cargo) {
      char rustdoc[4096];
      if(snprintf(rustdoc,sizeof(rustdoc),"%s/bin/rustdoc%s",sysroot,IsWindows()?".exe":"")>=(int)sizeof(rustdoc))return 1;
      if(setenv("RUSTC",rustc,1) || setenv("RUSTDOC",rustdoc,1)) {
        perror("set packaged Rust tools");return 1;
      }
    }
    char **rargs=calloc((size_t)argc+7,sizeof(char*));
    if (!rargs) return 1;
    int n=0;
    if(IsWindows()) {
      rargs[n++]="sh.exe";
      rargs[n++]="-c";
      rargs[n++]=WINDOWS_SHELL_RESET "exec \"$0\" \"$@\"";
    }
    rargs[n++]=is_cargo?cargo:rustc;
    if(!is_cargo) {
      rargs[n++]="--sysroot";
      rargs[n++]=sysroot;
    }
    for(int i=3;i<argc;i++)rargs[n++]=argv[i];
    rargs[n]=NULL;
    rc=run_external(rargs);
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
  /* CreateProcess does not interpret shebangs, and commandv searches exact
   * filenames rather than applying Windows PATHEXT suffixes. */
  args[n++]=IsWindows()?"sh.exe":"sh";
  if(IsWindows()) {
    /* execve adds private Cosmo process/descriptor handoff variables. The
     * external shell cannot consume them; clear them there, after execve,
     * so APE grandchildren use the shell's actual redirected Win32 handles. */
    args[n++]="-c";
    args[n++]=WINDOWS_SHELL_RESET ". \"$0\"";
  }
  args[n++]=compiler;
  if(is_cpp) {
    args[n++]="-I";
    args[n++]=headers;
  }
  for(int i=2;i<argc;i++) args[n++]=argv[i];
  if(is_cpp && !compile_only) args[n++]="-lstdc++";
  args[n]=NULL;
  rc=run_external(args);
  free(args);
  return rc;
}
