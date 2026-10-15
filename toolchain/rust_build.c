/* Minimal Rust no_std -> multiarch Cosmopolitan APE build driver.
 * Contract: Rust source exports extern "C" fn rust_main() -> i32.
 * Uses the embedded compiler for both architectures and final linking. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <limits.h>
#include <errno.h>

static int invoke(char *const args[]) {
  pid_t p=fork();
  if(p<0) {perror("fork");return 125;}
  if(!p) {execv(args[0],args);perror("execv");_exit(127);}
  int st;
  if(waitpid(p,&st,0)<0) {perror("waitpid");return 125;}
  return WIFEXITED(st)?WEXITSTATUS(st):128+WTERMSIG(st);
}
int rust_build_main(int argc,char **argv) {
  if(argc<4 || strcmp(argv[2],"build")) {
    fputs("Usage: toolchain.com rust build input.rs [-o output.com]\n",stderr);
    return 2;
  }
  const char *source=argv[3],*output="a.com";
  for(int i=4;i<argc;i++) {
    if(!strcmp(argv[i],"-o") && i+1<argc)output=argv[++i];
    else {fprintf(stderr,"Unknown Rust build option: %s\n",argv[i]);return 2;}
  }
  char self[4096],dir[]="/tmp/rust-ape-build-XXXXXX";
  if(!realpath(argv[0],self)) {perror("resolve APE");return 1;}
  if(!mkdtemp(dir)) {perror("mkdtemp");return 1;}
  char x[4096],a[4096],subdir[4096],wrapper[4096],cobj[4096];
  if(snprintf(x,sizeof(x),"%s/rust.o",dir)>=(int)sizeof(x) ||
     snprintf(subdir,sizeof(subdir),"%s/.aarch64",dir)>=(int)sizeof(subdir) ||
     snprintf(a,sizeof(a),"%s/rust.o",subdir)>=(int)sizeof(a) ||
     snprintf(wrapper,sizeof(wrapper),"%s/main.c",dir)>=(int)sizeof(wrapper) ||
     snprintf(cobj,sizeof(cobj),"%s/main.o",dir)>=(int)sizeof(cobj))return 1;
  if(mkdir(subdir,0700)) {perror("mkdir");return 1;}
  char *xargs[]={self,"rust","rustc","--edition=2024","--crate-type=lib","--emit=obj",
    "-C","panic=abort","-C","overflow-checks=off",(char*)source,"-o",x,NULL};
  int rc=invoke(xargs);
  if(!rc) {
    char *aargs[]={self,"rust","rustc","--edition=2024","--target","aarch64-unknown-linux-gnu",
      "--crate-type=lib","--emit=obj","-C","panic=abort","-C","overflow-checks=off",
      (char*)source,"-o",a,NULL};
    rc=invoke(aargs);
  }
  if(!rc) {
    FILE *f=fopen(wrapper,"wb");
    if(!f) {perror("wrapper");rc=1;}
    else {
      fputs("extern int rust_main(void);\nint main(void){return rust_main();}\n",f);
      if(fclose(f))rc=1;
    }
  }
  if(!rc) {
    char *compile[]={self,"cc","-c",wrapper,"-o",cobj,NULL};
    rc=invoke(compile);
  }
  if(!rc) {
    char *link[]={self,"cc",cobj,x,"-o",(char*)output,NULL};
    rc=invoke(link);
  }
  if(!rc)fprintf(stderr,"Built Rust APE: %s\n",output);
  /* Keep temporary objects for diagnostics until cleanup support is added. */
  return rc;
}
