/* Experimental SDK launcher. Requires host python3 for ZIP extraction.
 * Not yet a dependency-free or in-process compiler. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <dirent.h>
#include <limits.h>

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
int embedded_sdk_main(int argc,char **argv) {
  if(argc<2) { fputs("Usage: toolchain.com cc [cosmocc arguments]\n",stderr); return 2; }
  char self[4096],tmp[]="/tmp/cosmo-sdk-XXXXXX",compiler[4096];
  if(!realpath(argv[0],self)) { perror("resolve executable"); return 1; }
  if(!mkdtemp(tmp)) { perror("mkdtemp"); return 1; }
  const char *script="import sys,zipfile\n"
    "z=zipfile.ZipFile(sys.argv[1]); z.extractall(sys.argv[2])\n"
    "import os,stat\n"
    "for i in z.infolist():\n"
    " p=os.path.join(sys.argv[2],i.filename)\n"
    " if os.path.isfile(p): os.chmod(p,(i.external_attr>>16)&0o777 or 0o644)\n";
  char *extract[]={"python3","-c",(char*)script,self,tmp,NULL};
  int rc=run(extract);
  if(rc) { fprintf(stderr,"SDK extraction failed: %d\n",rc); return rc; }
  if(!find_cc(tmp,compiler,sizeof(compiler),0)) {
    fputs("No cosmocc compiler found in embedded SDK\n",stderr); return 1;
  }
  char **args=calloc((size_t)argc+1,sizeof(char*));
  if(!args) return 1;
  args[0]=compiler;
  for(int i=2;i<argc;i++) args[i-1]=argv[i];
  rc=run(args);
  free(args);
  /* Temporary SDK directory retained for diagnostics and compiler subprocesses. */
  fprintf(stderr,"SDK extracted at %s (temporary bootstrap mode)\n",tmp);
  return rc;
}
