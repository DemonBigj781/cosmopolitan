/* One-command Machin -> C -> APE driver. Subprocesses invoke the SAME APE. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <limits.h>
#include <errno.h>

static int child(char *const args[], const char *stdout_path) {
  pid_t pid=fork();
  if(pid<0){perror("fork");return 125;}
  if(pid==0){
    if(stdout_path){
      int fd=open(stdout_path,O_WRONLY|O_CREAT|O_TRUNC,0600);
      if(fd<0){perror("open generated C");_exit(126);}
      if(dup2(fd,STDOUT_FILENO)<0){perror("dup2");_exit(126);}
      close(fd);
    }
    execv(args[0],args);
    perror("execv");_exit(127);
  }
  int status;
  if(waitpid(pid,&status,0)<0){perror("waitpid");return 125;}
  if(WIFEXITED(status))return WEXITSTATUS(status);
  return WIFSIGNALED(status)?128+WTERMSIG(status):125;
}
int machin_build_main(int argc,char **argv){
  if(argc<4 || strcmp(argv[2],"build")){
    fputs("Usage: toolchain.com machin build input.mfl [-o output.com]\\n",stderr);
    return 2;
  }
  const char *source=argv[3],*output="a.com";
  for(int i=4;i<argc;i++){
    if(!strcmp(argv[i],"-o")&&i+1<argc)output=argv[++i];
    else {fprintf(stderr,"Unknown build argument: %s\\n",argv[i]);return 2;}
  }
  char self[4096];
  if(!realpath(argv[0],self)){perror("resolve APE");return 1;}
  char tempdir[]="/tmp/machin-ape-XXXXXX";
  if(!mkdtemp(tempdir)){perror("mkdtemp");return 1;}
  char temp[4096];
  if(snprintf(temp,sizeof(temp),"%s/generated.c",tempdir)>=(int)sizeof(temp))return 1;
  char *generate[]={self,"machin-compiler","--full",(char*)source,NULL};
  int rc=child(generate,temp);
  if(!rc){
    char *compile[]={self,"cc","-O2","-std=c11","-pthread",
                     temp,"-o",(char*)output,NULL};
    rc=child(compile,NULL);
  }
  if(unlink(temp)&&errno!=ENOENT)perror("remove intermediate C");
  if(rmdir(tempdir))perror("remove temporary directory");
  if(!rc)fprintf(stderr,"Built %s using the unified APE\\n",output);
  return rc;
}
