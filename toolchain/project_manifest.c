/* Build student APEs from one JSON project manifest. Public invocation:
 * cosmo.com path/to/project/manifest.json */
#include <cosmo.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "third_party/lua/lauxlib.h"
#include "third_party/lua/lua.h"
#include "tool/net/ljson.h"
#include "sdk_resources.h"

#define MANIFEST_LIMIT (1024 * 1024)
#define ARGUMENT_LIMIT 2048

extern int machin_build_main(int, char **) __attribute__((weak));
extern int rust_build_main(int, char **) __attribute__((weak));

struct Strings {
  char **items;
  size_t count;
  size_t capacity;
};

struct Libraries {
  struct Strings self;
  struct Strings file;
  struct Strings folder;
  struct Strings git;
  struct Strings ftp;
  struct Strings http;
  struct Strings https;
};

struct Project {
  char *language;
  char *target;
  char *output;
  struct Strings sources;
  struct Libraries libraries;
};

static void strings_free(struct Strings *strings) {
  for(size_t i=0;i<strings->count;i++)free(strings->items[i]);
  free(strings->items);
  memset(strings,0,sizeof(*strings));
}

static int strings_push_owned(struct Strings *strings,char *value) {
  if(strings->count>=ARGUMENT_LIMIT) {free(value);errno=E2BIG;return -1;}
  if(strings->count==strings->capacity) {
    size_t capacity=strings->capacity?strings->capacity*2:16;
    char **items=realloc(strings->items,capacity*sizeof(*items));
    if(!items) {free(value);return -1;}
    strings->items=items;strings->capacity=capacity;
  }
  strings->items[strings->count++]=value;
  return 0;
}

static int strings_push(struct Strings *strings,const char *value) {
  char *copy=strdup(value);
  return copy?strings_push_owned(strings,copy):-1;
}

static int strings_pushf(struct Strings *strings,const char *format,...) {
  va_list ap,copy;
  va_start(ap,format);va_copy(copy,ap);
  int size=vsnprintf(NULL,0,format,copy);
  va_end(copy);
  if(size<0) {va_end(ap);return -1;}
  char *text=malloc((size_t)size+1);
  if(!text) {va_end(ap);return -1;}
  vsnprintf(text,(size_t)size+1,format,ap);va_end(ap);
  return strings_push_owned(strings,text);
}

static void project_free(struct Project *project) {
  free(project->language);free(project->target);free(project->output);
  strings_free(&project->sources);
  strings_free(&project->libraries.self);
  strings_free(&project->libraries.file);
  strings_free(&project->libraries.folder);
  strings_free(&project->libraries.git);
  strings_free(&project->libraries.ftp);
  strings_free(&project->libraries.http);
  strings_free(&project->libraries.https);
  memset(project,0,sizeof(*project));
}

static char *read_manifest(const char *path,size_t *size) {
  int fd=open(path,O_RDONLY|O_CLOEXEC);
  if(fd<0)return NULL;
  struct stat st;
  if(fstat(fd,&st) || st.st_size<0 || st.st_size>MANIFEST_LIMIT) {
    int error=errno?errno:EFBIG;close(fd);errno=error;return NULL;
  }
  char *data=malloc((size_t)st.st_size+1);
  if(!data) {close(fd);return NULL;}
  size_t done=0;
  while(done<(size_t)st.st_size) {
    ssize_t got=read(fd,data+done,(size_t)st.st_size-done);
    if(got<0 && errno==EINTR)continue;
    if(got<=0) {int error=got?errno:EIO;free(data);close(fd);errno=error;return NULL;}
    done+=(size_t)got;
  }
  if(close(fd)) {int error=errno;free(data);errno=error;return NULL;}
  data[done]=0;*size=done;return data;
}

static int table_string_field(lua_State *L,int table,const char *name,char **result,int required) {
  table=lua_absindex(L,table);lua_getfield(L,table,name);
  if(lua_isnil(L,-1)) {
    lua_pop(L,1);
    if(required)fprintf(stderr,"manifest.%s is required\n",name);
    return required?-1:0;
  }
  if(lua_type(L,-1)!=LUA_TSTRING) {
    fprintf(stderr,"manifest.%s must be a string\n",name);lua_pop(L,1);return -1;
  }
  const char *value=lua_tostring(L,-1);
  *result=strdup(value);lua_pop(L,1);
  return *result?0:-1;
}

static int collect_string_values(lua_State *L,int value,const char *field,struct Strings *output) {
  value=lua_absindex(L,value);
  if(lua_type(L,value)==LUA_TSTRING)return strings_push(output,lua_tostring(L,value));
  if(!lua_istable(L,value)) {
    fprintf(stderr,"%s must be a string or string array\n",field);return -1;
  }
  size_t length=lua_rawlen(L,value),keys=0;
  lua_pushnil(L);
  while(lua_next(L,value)) {
    int valid=lua_isinteger(L,-2) &&
              ((length==0 && lua_tointeger(L,-2)==0 && lua_isboolean(L,-1) && !lua_toboolean(L,-1)) ||
               (lua_tointeger(L,-2)>=1 && (size_t)lua_tointeger(L,-2)<=length));
    lua_pop(L,1);
    if(!valid) {
      fprintf(stderr,"%s must be an array\n",field);lua_pop(L,1);return -1;
    }
    keys++;
  }
  if(keys!=length+(length==0)) {
    fprintf(stderr,"%s must be a dense array\n",field);return -1;
  }
  for(size_t i=1;i<=length;i++) {
    lua_rawgeti(L,value,(lua_Integer)i);
    if(lua_type(L,-1)!=LUA_TSTRING) {
      fprintf(stderr,"%s[%zu] must be a string\n",field,i);
      lua_pop(L,1);return -1;
    }
    int rc=strings_push(output,lua_tostring(L,-1));lua_pop(L,1);
    if(rc)return -1;
  }
  return 0;
}

static int validate_object_keys(lua_State *L,int table,const char *scope,
                                const char *const allowed[],size_t allowed_count) {
  table=lua_absindex(L,table);
  lua_pushnil(L);
  while(lua_next(L,table)) {
    if(lua_type(L,-2)!=LUA_TSTRING) {
      fprintf(stderr,"%s keys must be strings\n",scope);lua_pop(L,2);return -1;
    }
    const char *name=lua_tostring(L,-2);int known=0;
    for(size_t i=0;i<allowed_count;i++)if(!strcmp(name,allowed[i])) {known=1;break;}
    lua_pop(L,1);
    if(!known) {
      fprintf(stderr,"unknown %s.%s\n",scope,name);lua_pop(L,1);return -1;
    }
  }
  return 0;
}

static struct Strings *library_values(struct Libraries *libraries,const char *name) {
  if(!strcmp(name,"self"))return &libraries->self;
  if(!strcmp(name,"file"))return &libraries->file;
  if(!strcmp(name,"folder"))return &libraries->folder;
  if(!strcmp(name,"git"))return &libraries->git;
  if(!strcmp(name,"ftp"))return &libraries->ftp;
  if(!strcmp(name,"http"))return &libraries->http;
  if(!strcmp(name,"https"))return &libraries->https;
  return NULL;
}

static int parse_libraries(lua_State *L,int manifest,struct Libraries *libraries) {
  static const char *const allowed[]={"self","file","folder","git","ftp","http","https"};
  manifest=lua_absindex(L,manifest);lua_getfield(L,manifest,"libraries");
  if(lua_isnil(L,-1)) {lua_pop(L,1);return 0;}
  if(!lua_istable(L,-1)) {
    fputs("manifest.libraries must be an object\n",stderr);lua_pop(L,1);return -1;
  }
  int table=lua_absindex(L,-1),rc=0;
  if(validate_object_keys(L,table,"manifest.libraries",allowed,sizeof(allowed)/sizeof(*allowed))) {
    lua_pop(L,1);return -1;
  }
  lua_pushnil(L);
  while(lua_next(L,table)) {
    if(lua_type(L,-2)!=LUA_TSTRING) {
      fputs("manifest.libraries keys must be strings\n",stderr);rc=-1;lua_pop(L,2);break;
    }
    const char *name=lua_tostring(L,-2);
    struct Strings *values=library_values(libraries,name);
    char field[64];
    if(snprintf(field,sizeof(field),"manifest.libraries.%s",name)>=(int)sizeof(field) ||
       collect_string_values(L,-1,field,values)) {rc=-1;lua_pop(L,2);break;}
    lua_pop(L,1);
  }
  lua_pop(L,1);return rc;
}

static int parse_sources(lua_State *L,int manifest,struct Strings *sources) {
  manifest=lua_absindex(L,manifest);lua_getfield(L,manifest,"sources");
  if(lua_isnil(L,-1)) {
    fputs("manifest.sources is required\n",stderr);lua_pop(L,1);return -1;
  }
  int rc=collect_string_values(L,-1,"manifest.sources",sources);lua_pop(L,1);
  if(!rc && !sources->count) {fputs("manifest.sources must not be empty\n",stderr);return -1;}
  return rc;
}

static int parse_project(const char *path,struct Project *project) {
  static const char *const root_fields[]={"manifest"};
  static const char *const manifest_fields[]={"language","target","output","sources","libraries"};
  size_t size;char *data=read_manifest(path,&size);
  if(!data) {perror(path);return -1;}
  lua_State *L=luaL_newstate();
  if(!L) {free(data);return -1;}
  struct DecodeJson decoded=DecodeJson(L,data,size);
  if(decoded.rc!=1) {
    fprintf(stderr,"invalid JSON manifest: %s\n",decoded.rc<0?decoded.p:"empty document");
    lua_close(L);free(data);return -1;
  }
  const char *end=decoded.p;
  while(end<data+size && isspace((unsigned char)*end))end++;
  if(end!=data+size || !lua_istable(L,-1)) {
    fputs(end!=data+size?"invalid JSON manifest: trailing data\n":"JSON root must be an object\n",stderr);
    lua_close(L);free(data);return -1;
  }
  if(validate_object_keys(L,-1,"JSON root",root_fields,sizeof(root_fields)/sizeof(*root_fields))) {
    lua_close(L);free(data);return -1;
  }
  lua_getfield(L,-1,"manifest");
  if(!lua_istable(L,-1)) {
    fputs("JSON root must contain a manifest object\n",stderr);
    lua_close(L);free(data);return -1;
  }
  int manifest=lua_absindex(L,-1),rc=0;
  if(validate_object_keys(L,manifest,"manifest",manifest_fields,
                          sizeof(manifest_fields)/sizeof(*manifest_fields))) {
    lua_close(L);free(data);return -1;
  }
  rc|=table_string_field(L,manifest,"language",&project->language,1);
  rc|=table_string_field(L,manifest,"target",&project->target,1);
  rc|=table_string_field(L,manifest,"output",&project->output,1);
  if(!rc)rc=parse_sources(L,manifest,&project->sources);
  if(!rc)rc=parse_libraries(L,manifest,&project->libraries);
  lua_close(L);free(data);
  return rc?-1:0;
}

static int absolute_path(const char *path) {
  return path[0]=='/' || (path[0] && path[1] && path[2] &&
                          isalpha((unsigned char)path[0]) && path[1]==':' &&
                          (path[2]=='/' || path[2]=='\\'));
}

static char *manifest_directory(const char *path) {
  char *copy=strdup(path);if(!copy)return NULL;
  char *slash=strrchr(copy,'/');
  if(!slash) {free(copy);return strdup(".");}
  if(slash==copy)slash[1]=0;else *slash=0;
  return copy;
}

static char *join_path(const char *directory,const char *path) {
  if(absolute_path(path))return strdup(path);
  size_t left=strlen(directory),right=strlen(path);
  char *joined=malloc(left+right+2);if(!joined)return NULL;
  memcpy(joined,directory,left);joined[left]='/';memcpy(joined+left+1,path,right+1);
  return joined;
}

static char *existing_path(const char *directory,const char *path,int directory_required) {
  char *joined=join_path(directory,path);if(!joined)return NULL;
  char *resolved=realpath(joined,NULL);free(joined);
  if(!resolved)return NULL;
  struct stat st;
  if(stat(resolved,&st) || (directory_required?!S_ISDIR(st.st_mode):!S_ISREG(st.st_mode))) {
    free(resolved);errno=directory_required?ENOTDIR:EINVAL;return NULL;
  }
  return resolved;
}

static int make_output_parent(const char *path) {
  char *copy=strdup(path);if(!copy)return -1;
  char *slash=strrchr(copy,'/');
  if(!slash) {free(copy);return 0;}
  *slash=0;
  for(char *p=copy+1;*p;p++)if(*p=='/') {
    *p=0;if(mkdir(copy,0755)&&errno!=EEXIST){free(copy);return -1;}*p='/';
  }
  int rc=*copy && mkdir(copy,0755) && errno!=EEXIST?-1:0;free(copy);return rc;
}

static int add_bundle_arguments(struct Strings *arguments,const char *root) {
  char path[4096];
  if(snprintf(path,sizeof(path),"%s/link.args",root)>=(int)sizeof(path))return -1;
  FILE *file=fopen(path,"r");
  if(!file) {perror(path);return -1;}
  char *line=NULL;size_t capacity=0;int rc=0;
  while(getline(&line,&capacity,file)>=0) {
    size_t length=strlen(line);
    while(length && (line[length-1]=='\n'||line[length-1]=='\r'))line[--length]=0;
    if(!length)continue;
    if(strchr(line,'\n')) {rc=-1;break;}
    if(line[0]=='-')rc=strings_push(arguments,line);
    else if(absolute_path(line))rc=strings_push(arguments,line);
    else rc=strings_pushf(arguments,"%s/%s",root,line);
    if(rc)break;
  }
  if(ferror(file))rc=-1;
  free(line);
  if(fclose(file))rc=-1;
  return rc;
}

static int remote_libraries(const struct Libraries *libraries) {
  const struct {const char *name;const struct Strings *values;} sources[]={
    {"git",&libraries->git},{"ftp",&libraries->ftp},
    {"http",&libraries->http},{"https",&libraries->https},
  };
  for(size_t i=0;i<sizeof(sources)/sizeof(*sources);i++)if(sources[i].values->count) {
    fprintf(stderr,"manifest library source '%s' is recognized but its embedded acquisition transport is not implemented yet: %s\n",
            sources[i].name,sources[i].values->items[0]);
    return 1;
  }
  return 0;
}

static int wants_webgpu(const struct Project *project) {
  int found=0;
  for(size_t i=0;i<project->libraries.self.count;i++) {
    if(strcmp(project->libraries.self.items[i],"webgpu")) {
      fprintf(stderr,"unknown embedded toolchain library: %s\n",project->libraries.self.items[i]);return -1;
    }
    if(found) {fputs("embedded toolchain library listed more than once: webgpu\n",stderr);return -1;}
    found=1;
  }
  return found;
}

static int patch_pe_stack(const char *path) {
  int fd=open(path,O_RDWR|O_CLOEXEC);if(fd<0)return -1;
  unsigned char head[64],pehead[96];
  if(pread(fd,head,sizeof(head),0)!=(ssize_t)sizeof(head) || memcmp(head,"MZ",2))goto bad;
  uint32_t pe=(uint32_t)head[60]|((uint32_t)head[61]<<8)|((uint32_t)head[62]<<16)|((uint32_t)head[63]<<24);
  if(pread(fd,pehead,sizeof(pehead),pe)!=(ssize_t)sizeof(pehead) || memcmp(pehead,"PE\0\0",4))goto bad;
  uint16_t machine=(uint16_t)pehead[4]|((uint16_t)pehead[5]<<8);
  uint16_t optional_size=(uint16_t)pehead[20]|((uint16_t)pehead[21]<<8);
  uint16_t magic=(uint16_t)pehead[24]|((uint16_t)pehead[25]<<8);
  if(machine!=0x8664 || optional_size<96 || magic!=0x20b)goto bad;
  off_t stack=(off_t)pe+24+72;unsigned char values[16];
  if(pread(fd,values,sizeof(values),stack)!=(ssize_t)sizeof(values))goto bad;
  uint64_t reserve=0,commit=0;
  for(int i=0;i<8;i++){reserve|=(uint64_t)values[i]<<(i*8);commit|=(uint64_t)values[i+8]<<(i*8);}
  if((reserve!=65536 && reserve!=8*1024*1024) || commit!=4096)goto bad;
  reserve=8*1024*1024;
  for(int i=0;i<8;i++)values[i]=(unsigned char)(reserve>>(i*8));
  int rc=pwrite(fd,values,8,stack)!=8 || fsync(fd);
  if(close(fd))rc=1;
  return rc?-1:0;
bad:
  close(fd);errno=EINVAL;return -1;
}

static int publish_output(const char *temporary,const char *output) {
  char backup[4096];int had_output=!access(output,F_OK);
  if(snprintf(backup,sizeof(backup),"%s.cosmo-backup-%ld",output,(long)getpid())>=(int)sizeof(backup))return -1;
  if(had_output) {
    struct stat st;
    if(lstat(output,&st) || !S_ISREG(st.st_mode)) {errno=EEXIST;return -1;}
  }
  if(had_output && rename(output,backup))return -1;
  if(rename(temporary,output)) {
    int error=errno;if(had_output)rename(backup,output);errno=error;return -1;
  }
  if(had_output && unlink(backup))return -1;
  return 0;
}

static int build_existing_language(const char *self,const char *directory,
                                   const struct Project *project,const char *output) {
  if(project->sources.count!=1) {
    fprintf(stderr,"manifest language %s currently requires exactly one source\n",project->language);return 2;
  }
  if(project->libraries.self.count || project->libraries.file.count ||
     project->libraries.folder.count || remote_libraries(&project->libraries)) {
    fprintf(stderr,"manifest libraries are not supported by the %s student linker yet\n",project->language);return 2;
  }
  if(strcmp(project->target,"ape")) {
    fprintf(stderr,"manifest target for %s must be 'ape'\n",project->language);return 2;
  }
  char *source=existing_path(directory,project->sources.items[0],0);
  if(!source) {perror(project->sources.items[0]);return 1;}
  char temporary[4096];
  if(snprintf(temporary,sizeof(temporary),"%s.cosmo-tmp-%ld",output,(long)getpid())>=(int)sizeof(temporary)) {free(source);return 1;}
  unlink(temporary);
  char *args[7]={ (char*)self, project->language, "build", source, "-o", temporary, NULL };
  int rc;
  if(!strcmp(project->language,"rust")) {
    if(!rust_build_main) {fputs("Rust build driver not linked\n",stderr);free(source);return 2;}
    rc=rust_build_main(6,args);
  } else {
    if(!machin_build_main) {fputs("Machin build driver not linked\n",stderr);free(source);return 2;}
    rc=machin_build_main(6,args);
  }
  free(source);
  if(!rc && publish_output(temporary,output)) {perror("publish student APE");rc=1;}
  if(rc)unlink(temporary);
  return rc;
}

static int build_c_project(const char *self,const char *directory,
                           const struct Project *project,const char *output) {
  int webgpu=wants_webgpu(project);if(webgpu<0)return 2;
  if(remote_libraries(&project->libraries))return 2;
  if(strcmp(project->target,"ape") && strcmp(project->target,"x86_64")) {
    fputs("manifest.target must be 'ape' or 'x86_64'\n",stderr);return 2;
  }
  if(webgpu && strcmp(project->target,"x86_64")) {
    fputs("embedded library webgpu currently requires manifest.target 'x86_64'\n",stderr);return 2;
  }
  if(strcmp(project->language,"c") && strcmp(project->language,"c++"))return 2;
  char resolved_self[4096];
  if(!realpath(self,resolved_self)) {perror("resolve toolchain APE");return 1;}
  if(embedded_sdk_materialize(resolved_self,SDK_C) ||
     (webgpu && embedded_sdk_materialize(resolved_self,SDK_WEBGPU)))return 1;
  const char *sdk=embedded_sdk_root();if(!sdk)return 1;
  int x86_only=!strcmp(project->target,"x86_64");
  char compiler[4096];
  struct Strings args={0};int rc=1;
  if(x86_only) {
    const char *driver=!strcmp(project->language,"c")?"cc":"c++";
    if(snprintf(compiler,sizeof(compiler),"%s/bin/x86_64-unknown-cosmo-%s",sdk,driver)>=(int)sizeof(compiler) ||
       access(compiler,X_OK)) {
      fputs("Packaged x86-64 compiler driver is missing\n",stderr);goto done;
    }
    if(strings_push(&args,compiler))goto done;
  } else if(strings_push(&args,resolved_self) || strings_push(&args,project->language))goto done;
  if(strings_push(&args,!strcmp(project->language,"c")?"-std=c11":"-std=c++17") ||
     strings_push(&args,"-O2") || strings_push(&args,"-g") ||
     strings_push(&args,"-fno-omit-frame-pointer") || strings_push(&args,"-fno-stack-protector") ||
     strings_push(&args,"-Wall") || strings_push(&args,"-Wextra") ||
     strings_push(&args,"-Wno-missing-field-initializers"))goto done;
  if(webgpu) {
    if(strings_push(&args,"-DCOSMO_WGPU_EMBEDDED_LAVAPIPE=1") ||
       strings_push(&args,"-I") || strings_pushf(&args,"%s/webgpu/include",sdk) ||
       strings_push(&args,"-I") || strings_pushf(&args,"%s/webgpu/runtime",sdk))goto done;
  }
  for(size_t i=0;i<project->sources.count;i++) {
    char *path=existing_path(directory,project->sources.items[i],0);
    if(!path) {perror(project->sources.items[i]);goto done;}
    if(strings_push_owned(&args,path))goto done;
  }
  for(size_t i=0;i<project->libraries.file.count;i++) {
    char *path=existing_path(directory,project->libraries.file.items[i],0);
    if(!path) {perror(project->libraries.file.items[i]);goto done;}
    if(strstr(path,".so") || strstr(path,".dylib") || strstr(path,".dll")) {
      fprintf(stderr,"shared library input is not portable: %s\n",path);free(path);goto done;
    }
    if(strings_push_owned(&args,path))goto done;
  }
  for(size_t i=0;i<project->libraries.folder.count;i++) {
    char *path=existing_path(directory,project->libraries.folder.items[i],1);
    if(!path) {perror(project->libraries.folder.items[i]);goto done;}
    char include[4096];
    if(snprintf(include,sizeof(include),"%s/include",path)<(int)sizeof(include) && !access(include,R_OK)) {
      if(strings_push(&args,"-I") || strings_push(&args,include)){free(path);goto done;}
    }
    if(add_bundle_arguments(&args,path)){free(path);goto done;}
    free(path);
  }
  if(webgpu) {
    if(strings_pushf(&args,"%s/webgpu/runtime/vulkan_loader.c",sdk) ||
       strings_pushf(&args,"%s/webgpu/runtime/win64_bridge.c",sdk))goto done;
  }
  if(webgpu) {
    char bundle[4096];
    if(snprintf(bundle,sizeof(bundle),"%s/webgpu",sdk)>=(int)sizeof(bundle) ||
       add_bundle_arguments(&args,bundle))goto done;
  }
  if(strings_push(&args,"-Wl,--gc-sections") || strings_push(&args,"-pthread") ||
     strings_push(&args,"-lm") || strings_push(&args,"-ldl"))goto done;
  char temporary[4096],debug[4096];
  if(snprintf(temporary,sizeof(temporary),"%s.cosmo-tmp-%ld",output,(long)getpid())>=(int)sizeof(temporary) ||
     snprintf(debug,sizeof(debug),"%s.dbg",temporary)>=(int)sizeof(debug))goto done;
  unlink(temporary);unlink(debug);
  if(strings_push(&args,"-o") || strings_push(&args,!strcmp(project->target,"x86_64")?debug:temporary) ||
     strings_push_owned(&args,NULL))goto done;
  rc=x86_only?embedded_sdk_run_script(args.items):embedded_sdk_main((int)args.count-1,args.items);
  if(rc)goto cleanup;
  if(!strcmp(project->target,"x86_64")) {
    char apelink[4096],loader[4096];
    if(snprintf(apelink,sizeof(apelink),"%s/bin/apelink",sdk)>=(int)sizeof(apelink) ||
       snprintf(loader,sizeof(loader),"%s/bin/ape-x86_64.elf",sdk)>=(int)sizeof(loader)) {rc=1;goto cleanup;}
    char *link_args[]={apelink,"-l",loader,"-o",temporary,debug,NULL};
    rc=embedded_sdk_run_script(link_args);
    if(!rc && webgpu && patch_pe_stack(temporary)) {perror("configure student PE stack");rc=1;}
  }
  if(!rc && chmod(temporary,0755)) {perror("mark student APE executable");rc=1;}
  if(!rc && publish_output(temporary,output)) {perror("publish student APE");rc=1;}
cleanup:
  unlink(debug);if(rc)unlink(temporary);
done:
  strings_free(&args);return rc;
}

int project_manifest_main(int argc,char **argv) {
  if(argc!=2) {fputs("Usage: cosmo.com path/to/project/manifest.json\n",stderr);return 2;}
  struct Project project={0};
  if(parse_project(argv[1],&project)) {project_free(&project);return 2;}
  char *directory=manifest_directory(argv[1]);
  char *output=directory?join_path(directory,project.output):NULL;
  if(!directory || !output) {free(directory);free(output);project_free(&project);return 1;}
  if(make_output_parent(output)) {perror("create student output directory");free(directory);free(output);project_free(&project);return 1;}
  int rc;
  if(!strcmp(project.language,"c") || !strcmp(project.language,"c++"))
    rc=build_c_project(argv[0],directory,&project,output);
  else if(!strcmp(project.language,"rust") || !strcmp(project.language,"machin"))
    rc=build_existing_language(argv[0],directory,&project,output);
  else {
    fprintf(stderr,"unsupported manifest language: %s\n",project.language);rc=2;
  }
  free(directory);free(output);project_free(&project);return rc;
}
