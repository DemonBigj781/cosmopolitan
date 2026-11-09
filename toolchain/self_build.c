#include <cosmo.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include "sdk_resources.h"

struct SourceSpec {
  const char *name;
  const char *path;
  int machin;
};

static const struct SourceSpec kSources[] = {
  {"ape_toolchain", "source/toolchain/ape_toolchain.c", 0},
  {"embedded-sdk-driver", "source/toolchain/embedded-sdk-driver.c", 0},
  {"native_zip", "source/toolchain/native_zip.c", 0},
  {"machin_build", "source/toolchain/machin_build.c", 0},
  {"project_manifest", "source/toolchain/project_manifest.c", 0},
  {"rust_build", "source/toolchain/rust_build.c", 0},
  {"self_build", "source/toolchain/self_build.c", 0},
  {"tempdir", "source/toolchain/tempdir.c", 0},
  {"compiler", "source/o/toolchain/compiler.c", 1},
};

static int join_path(char *out,size_t size,const char *root,const char *relative) {
  return snprintf(out,size,"%s/%s",root,relative)>=(int)size ? -1 : 0;
}

static char **ape_command(const char *root,const char *tool,char *const extra[]) {
  int count=0;
  while(extra[count])count++;
  char **argv=calloc((size_t)count+4,sizeof(char*));
  if(!argv)return NULL;
  char *program=malloc(PATH_MAX),*loader=NULL;
  if(!program || (tool[0]=='/' ?
      snprintf(program,PATH_MAX,"%s",tool)>=(int)PATH_MAX :
      join_path(program,PATH_MAX,root,tool)))goto fail;
  int n=0;
  if(IsLinux()) {
#if defined(__aarch64__)
    const char *loader_name="bin/ape-aarch64.elf";
#else
    const char *loader_name="bin/ape-x86_64.elf";
#endif
    loader=malloc(PATH_MAX);
    if(!loader||join_path(loader,PATH_MAX,root,loader_name))goto fail;
    argv[n++]=loader;
    argv[n++]=program;
  }else{
    argv[n++]=program;
  }
  for(int i=0;i<count;i++)argv[n++]=extra[i];
  argv[n]=NULL;
  return argv;
fail:
  free(program);free(loader);free(argv);return NULL;
}

static void free_ape_command(char **argv) {
  if(!argv)return;
  if(IsLinux()) {free(argv[0]);free(argv[1]);}
  else free(argv[0]);
  free(argv);
}

static int run_tool(const char *root,const char *tool,char *const extra[]) {
  char **argv=ape_command(root,tool,extra);
  if(!argv)return 1;
  int rc=embedded_sdk_run(argv);
  free_ape_command(argv);
  return rc;
}

static int run_redirect(char *const argv[],const char *output) {
  pid_t child=fork();
  if(child<0)return 125;
  if(!child) {
    int fd=open(output,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
    if(fd<0||dup2(fd,1)<0)_exit(125);
    close(fd);
    execvp(argv[0],argv);
    _exit(127);
  }
  int status;
  pid_t waited;
  do { waited=waitpid(child,&status,0); } while(waited<0&&errno==EINTR);
  if(waited<0)return 125;
  return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}

static int sha256_file(const char *root,const char *path,const char *scratch,
                       char digest[65]) {
  unlink(scratch);
  char *extra[]={(char*)path,NULL};
  char **argv=ape_command(root,"bin/sha256sum.ape",extra);
  if(!argv)return 1;
  int rc=run_redirect(argv,scratch);
  free_ape_command(argv);
  if(rc)return rc;
  FILE *file=fopen(scratch,"rb");
  if(!file)return 1;
  size_t got=fread(digest,1,64,file);
  int invalid=ferror(file)||got!=64;
  if(fclose(file))invalid=1;
  unlink(scratch);
  for(int i=0;i<64&&!invalid;i++)if(!isxdigit((unsigned char)digest[i]))invalid=1;
  digest[64]=0;
  return invalid;
}

static int make_directory(const char *path) {
  return mkdir(path,0700)&&errno!=EEXIST ? -1 : 0;
}

static int compile_object(const char *root,const char *arch,
                          const struct SourceSpec *source) {
  int arm=!strcmp(arch,"aarch64"),n=0;
  char compiler[PATH_MAX],tlscc[PATH_MAX],output[PATH_MAX],seed[128];
  if(join_path(compiler,sizeof(compiler),root,
               arm?"bin/aarch64-linux-cosmo-gcc":"bin/x86_64-linux-cosmo-gcc")||
     join_path(tlscc,sizeof(tlscc),root,"self-build/tools/tlscc")||
     snprintf(output,sizeof(output),"work/%s/%s.o",arch,source->name)>=(int)sizeof(output)||
     snprintf(seed,sizeof(seed),"-frandom-seed=ape-toolchain-%s-%s",arch,source->name)>=(int)sizeof(seed))return 1;
  char *args[80];
  args[n++]="-V9";args[n++]="-M2048m";args[n++]="-P8192";args[n++]="-AOBJECTIFY.c";
  if(!arm)args[n++]=tlscc;
  args[n++]=compiler;
  args[n++]="-Wall";args[n++]="-Werror";args[n++]="-fno-omit-frame-pointer";
  args[n++]="-O2";args[n++]="-fno-schedule-insns2";
  args[n++]="-fno-optimize-sibling-calls";args[n++]="-mno-omit-leaf-frame-pointer";
  args[n++]=arm?"-fpatchable-function-entry=9,8":"-fpatchable-function-entry=18,16";
  args[n++]="-Wa,-W";args[n++]="-Wa,-I.";args[n++]="-Wa,--noexecstack";
  args[n++]="-D_COSMO_SOURCE";args[n++]=arm?"-DMODE=\"aarch64\"":"-DMODE=\"\"";
  args[n++]="-Wno-prio-ctor-dtor";args[n++]="-Wno-unknown-pragmas";
  args[n++]="-nostdinc";args[n++]="-iquote";args[n++]="source";
  args[n++]="-isystem";args[n++]="source/libc/isystem";
  args[n++]="-include";args[n++]="libc/integral/normalize.inc";
  args[n++]="-DSYSDEBUG";args[n++]="-DFTRACE";args[n++]="-fno-ident";
  args[n++]="-fno-common";args[n++]="-fno-gnu-unique";args[n++]="-fstrict-aliasing";
  args[n++]="-fstrict-overflow";args[n++]="-fno-semantic-interposition";
  args[n++]="-fno-record-gcc-switches";args[n++]=seed;
  if(arm) {args[n++]="-ffixed-x18";args[n++]="-ffixed-x28";args[n++]="-fsigned-char";}
  else {args[n++]="-mno-red-zone";args[n++]="-mno-tls-direct-seg-refs";}
  args[n++]="-std=gnu23";args[n++]="-fno-inline-functions-called-once";
  if(source->machin) {
    args[n++]="-std=c11";args[n++]="-pthread";args[n++]="-Dmain=machin_entry";
    args[n++]="-Wno-misleading-indentation";args[n++]="-Wno-unused-variable";
  }
  args[n++]="-c";args[n++]="-o";args[n++]=output;args[n++]=(char*)source->path;args[n]=NULL;
  if(run_tool(root,"bin/compile.ape",args))return 1;
  char *fixup[]={(char*)output,NULL};
  return run_tool(root,"self-build/tools/fixupobj",fixup);
}

static int link_architecture(const char *root,const char *arch) {
  char response[128],output[128];
  if(snprintf(response,sizeof(response),"@link/%s.rsp",arch)>=(int)sizeof(response)||
     snprintf(output,sizeof(output),"work/toolchain.%s.elf",arch)>=(int)sizeof(output))return 1;
  char *link_args[]={response,"-o",output,NULL};
  if(run_tool(root,!strcmp(arch,"aarch64")?"bin/aarch64-linux-cosmo-ld.bfd":
              "bin/x86_64-linux-cosmo-ld.bfd",link_args))return 1;
  char *fixup[]={(char*)output,NULL};
  return run_tool(root,"self-build/tools/fixupobj",fixup);
}

static int parse_generation(const char *path) {
  FILE *file=fopen(path,"rb");
  if(!file)return -1;
  char buffer[65536];
  size_t size=fread(buffer,1,sizeof(buffer)-1,file);
  int invalid=ferror(file);
  fclose(file);
  if(invalid)return -1;
  buffer[size]=0;
  char *field=strstr(buffer,"\"generation\"");
  if(!field||(field=strchr(field,':'))==NULL)return -1;
  char *end;
  long value=strtol(field+1,&end,10);
  return end==field+1||value<0||value>INT_MAX ? -1 : (int)value;
}

static int next_generation(const char *path,int *generation) {
  if(access(path,F_OK)) {
    if(errno==ENOENT) {*generation=0;return 0;}
    return -1;
  }
  int parent=parse_generation(path);
  if(parent<0 || parent==INT_MAX) {errno=EINVAL;return -1;}
  *generation=parent+1;
  return 0;
}

static void json_string(FILE *file,const char *value) {
  fputc('"',file);
  for(const unsigned char *p=(const unsigned char*)value;*p;p++) {
    if(*p=='"'||*p=='\\')fprintf(file,"\\%c",*p);
    else if(*p=='\n')fputs("\\n",file);
    else if(*p<32)fprintf(file,"\\u%04x",*p);
    else fputc(*p,file);
  }
  fputc('"',file);
}

static void json_key(FILE *file,const char *name) {
  json_string(file,name);fputs(": ",file);
}

static int write_provenance(const char *path,int generation,const char *parent,
                            const char *parent_hash,const char *artifact_hash,
                            long long artifact_size,const char *manifest_hash,
                            const char *sdk_hash) {
  int fd=open(path,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
  if(fd<0)return 1;
  FILE *file=fdopen(fd,"w");
  if(!file){close(fd);return 1;}
  fputs("{\n  ",file);json_key(file,"schema_version");fputs("1,\n  ",file);
  json_key(file,"generation");fprintf(file,"%d,\n  ",generation);
  json_key(file,"parent_path");json_string(file,parent);fputs(",\n  ",file);
  json_key(file,"parent_sha256");json_string(file,parent_hash);fputs(",\n  ",file);
  json_key(file,"artifact_sha256");json_string(file,artifact_hash);fputs(",\n  ",file);
  json_key(file,"artifact_size");fprintf(file,"%lld,\n  ",artifact_size);
  json_key(file,"self_build_manifest_sha256");json_string(file,manifest_hash);fputs(",\n  ",file);
  json_key(file,"sdk_metadata_sha256");json_string(file,sdk_hash);fputs(",\n  ",file);
  json_key(file,"target");json_string(file,"cosmopolitan-fat-ape-x86_64-aarch64");
  fputs("\n}\n",file);
  return fclose(file)!=0;
}

static int absolute_output(const char *cwd,const char *input,char *output,size_t size) {
  if(!*input) {errno=EINVAL;return -1;}
  if(input[0]=='/')return snprintf(output,size,"%s",input)>=(int)size ? -1 : 0;
  return snprintf(output,size,"%s/%s",cwd,input)>=(int)size ? -1 : 0;
}

int self_build_main(int argc,char **argv) {
  if(argc!=3) {
    fprintf(stderr,"Usage: %s self-build OUTPUT\n",argv[0]);
    return 2;
  }
  int rc=1,renamed=0,generation;
  char self[PATH_MAX],cwd[PATH_MAX],output[PATH_MAX],sidecar[PATH_MAX];
  char staging[PATH_MAX],sidecar_stage[PATH_MAX],parent_sidecar[PATH_MAX];
  char kit[PATH_MAX],work[PATH_MAX],hash_output[PATH_MAX],manifest[PATH_MAX],sdk_metadata[PATH_MAX];
  char parent_hash[65],artifact_hash[65],manifest_hash[65],sdk_hash[65];
  if(!realpath(argv[0],self)||!getcwd(cwd,sizeof(cwd))||
     absolute_output(cwd,argv[2],output,sizeof(output))||
     snprintf(sidecar,sizeof(sidecar),"%s.provenance.json",output)>=(int)sizeof(sidecar)||
     snprintf(staging,sizeof(staging),"%s.tmp.%ld",output,(long)getpid())>=(int)sizeof(staging)||
     snprintf(sidecar_stage,sizeof(sidecar_stage),"%s.tmp.%ld",sidecar,(long)getpid())>=(int)sizeof(sidecar_stage)||
     snprintf(parent_sidecar,sizeof(parent_sidecar),"%s.provenance.json",self)>=(int)sizeof(parent_sidecar)) {
    perror("resolve self-build paths");return 1;
  }
  if(!strcmp(self,output)||!access(output,F_OK)||!access(sidecar,F_OK)||
     !access(staging,F_OK)||!access(sidecar_stage,F_OK)) {
    fputs("self-build output and sidecar must not already exist or replace the parent\n",stderr);
    return 1;
  }
  if(embedded_sdk_materialize(self,SDK_ALL))return 1;
  const char *root=embedded_sdk_root();
  if(!root||join_path(kit,sizeof(kit),root,"self-build")||
     join_path(work,sizeof(work),kit,"work")||
     join_path(hash_output,sizeof(hash_output),work,"sha256.txt")||
     join_path(manifest,sizeof(manifest),root,"self-build/manifest.json")||
     join_path(sdk_metadata,sizeof(sdk_metadata),root,"toolchain-metadata/BUILD.json"))return 1;
  char x86[PATH_MAX],arm[PATH_MAX];
  if(join_path(x86,sizeof(x86),work,"x86_64")||join_path(arm,sizeof(arm),work,"aarch64")||
     make_directory(work)||make_directory(x86)||make_directory(arm)) {
    perror("create self-build workspace");goto done;
  }
  if(sha256_file(root,self,hash_output,parent_hash)||
     sha256_file(root,manifest,hash_output,manifest_hash)||
     sha256_file(root,sdk_metadata,hash_output,sdk_hash)) {
    fputs("self-build input hashing failed\n",stderr);goto done;
  }
  if(next_generation(parent_sidecar,&generation)) {
    perror("read parent self-build provenance");goto done;
  }
  if(chdir(kit)) {perror("enter self-build kit");goto done;}
  for(size_t i=0;i<sizeof(kSources)/sizeof(kSources[0]);i++)
    if(compile_object(root,"x86_64",&kSources[i]))goto restore;
  for(size_t i=0;i<sizeof(kSources)/sizeof(kSources[0]);i++)
    if(compile_object(root,"aarch64",&kSources[i]))goto restore;
  if(link_architecture(root,"x86_64")||link_architecture(root,"aarch64"))goto restore;
  {
    char loader_x86[PATH_MAX],loader_arm[PATH_MAX],model[PATH_MAX];
    if(join_path(loader_x86,sizeof(loader_x86),root,"bin/ape-x86_64.elf")||
       join_path(loader_arm,sizeof(loader_arm),root,"bin/ape-aarch64.elf")||
       join_path(model,sizeof(model),root,"bin/ape-m1.c"))goto restore;
    char *args[]={"-V","-1","-s","-l",loader_x86,"-l",loader_arm,"-M",model,
                  "-o",staging,"work/toolchain.x86_64.elf","work/toolchain.aarch64.elf",NULL};
    if(run_tool(root,"bin/apelink",args))goto restore;
  }
  {
    char *args[]={staging,NULL};
    if(run_tool(root,"bin/pecheck",args))goto restore;
  }
  {
    char *args[]={self,staging,NULL};
    if(run_tool(root,"bin/zipcopy",args))goto restore;
  }
  if(chmod(staging,0755)) {perror("make self-built APE executable");goto restore;}
  {
    char *args[]={"selftest",NULL};
    if(run_tool(root,staging,args)) {
      fputs("self-built APE failed its dispatcher self-test\n",stderr);goto restore;
    }
  }
  if(sha256_file(root,staging,hash_output,artifact_hash))goto restore;
  struct stat st;
  if(stat(staging,&st))goto restore;
  if(write_provenance(sidecar_stage,generation,self,parent_hash,artifact_hash,
                      (long long)st.st_size,manifest_hash,sdk_hash))goto restore;
  if(chdir(cwd)) {perror("restore working directory");goto done;}
  if(rename(staging,output)) {perror("publish self-built APE");goto done;}
  renamed=1;
  if(rename(sidecar_stage,sidecar)) {perror("publish self-build provenance");goto done;}
  printf("PASS: self-build generation %d %s %s\n",generation,artifact_hash,output);
  return 0;
restore:
  if(chdir(cwd))perror("restore working directory");
done:
  unlink(staging);unlink(sidecar_stage);
  if(renamed)unlink(output);
  return rc;
}
