/* Minimal ZIP32 extractor for the trusted, build-generated SDK archive.
 * Supports stored/deflated files and Unix symlinks. Rejects traversal.
 * ZIP64, encrypted entries and multi-disk archives are unsupported. */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>
#include "third_party/zlib/zlib.h"

static uint16_t u16(const unsigned char *p){return (uint16_t)p[0]|((uint16_t)p[1]<<8);}
static uint32_t u32(const unsigned char *p){return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static int read_at(FILE *f,uint64_t off,void *p,size_t n){
  return off<=LONG_MAX && !fseek(f,(long)off,SEEK_SET) && fread(p,1,n,f)==n;
}
static int dirs(const char *path){
  char b[4096]; size_t n=strlen(path);
  if(n>=sizeof(b))return -1;
  memcpy(b,path,n+1);
  for(char *p=b+1;*p;p++)if(*p=='/'){
    *p=0;if(mkdir(b,0700)&&errno!=EEXIST)return -1;*p='/';
  }
  return 0;
}
static int safe(const char *s){
  if(!*s||*s=='/'||strchr(s,'\\'))return 0;
  for(const char *p=s;*p;){
    const char *q=strchr(p,'/');size_t n=q?(size_t)(q-p):strlen(p);
    if(!n||(n==1&&p[0]=='.')||(n==2&&p[0]=='.'&&p[1]=='.'))return 0;
    if(!q)break;p=q+1;
    if(!*p)break;
  }
  return 1;
}
/* Validate relative symlink targets lexically against the SDK root.
 * name is the link location relative to root; target may use .. within root. */
static int safe_link_target(const char *name,const char *target){
  if(!*target||*target=='/'||strchr(target,'\\\\'))return 0;
  int depth=0;
  for(const char *p=name;*p;){
    const char *q=strchr(p,'/');if(!q)break;
    if(q>p)depth++;
    p=q+1;
  }
  for(const char *p=target;*p;){
    const char *q=strchr(p,'/');size_t n=q?(size_t)(q-p):strlen(p);
    if(n==2&&p[0]=='.'&&p[1]=='.'){if(--depth<0)return 0;}
    else if(n&&!(n==1&&p[0]=='.'))depth++;
    if(!q)break;
    p=q+1;
  }
  return 1;
}
static int inflate_entry(FILE *f,uint64_t off,uint32_t clen,uint32_t ulen,
                         int method,unsigned char **result){
  if(ulen>512*1024*1024u)return -1;
  unsigned char *out=malloc((size_t)ulen+1),*in=malloc((size_t)clen+1);
  if(!out||!in){free(out);free(in);return -1;}
  if(!read_at(f,off,in,clen)){free(out);free(in);return -1;}
  int ok=0;
  if(method==0){if(clen==ulen){memcpy(out,in,ulen);ok=1;}}
  else if(method==8){
    z_stream z={0};
    if(inflateInit2(&z,-MAX_WBITS)==Z_OK){
      z.next_in=in;z.avail_in=clen;z.next_out=out;z.avail_out=ulen?ulen:1;
      int status=inflate(&z,Z_FINISH);
      ok=status==Z_STREAM_END && z.total_out==ulen;
      inflateEnd(&z);
    }
  }
  free(in);
  if(!ok){free(out);return -1;}
  out[ulen]=0;*result=out;return 0;
}
int extract_sdk_zip(const char *archive,const char *root){
  FILE *f=fopen(archive,"rb");if(!f)return -1;
  int rc=-1;unsigned char end[22],head[46],local[30];
  if(fseek(f,0,SEEK_END))goto done;
  long size=ftell(f);if(size<22)goto done;
  long start=size>65557?size-65557:0, eocd=-1;
  for(long p=size-22;p>=start;p--){
    unsigned char sig[4];if(!read_at(f,p,sig,4))goto done;
    if(u32(sig)==0x06054b50){eocd=p;break;}
  }
  if(eocd<0||!read_at(f,eocd,end,22))goto done;
  if(u16(end+4)||u16(end+6)||u16(end+8)!=u16(end+10))goto done;
  uint32_t count=u16(end+10),cdsize=u32(end+12),cdoff=u32(end+16);
  if((uint64_t)cdoff+cdsize>(uint64_t)size){fputs("ZIP: invalid central directory bounds\n",stderr);goto done;}
  uint64_t pos=cdoff;
  for(uint32_t i=0;i<count;i++){
    if(!read_at(f,pos,head,46)||u32(head)!=0x02014b50){fprintf(stderr,"ZIP: central header failed at entry %u\n",i);goto done;}
    uint16_t flags=u16(head+8),method=u16(head+10),nl=u16(head+28),el=u16(head+30),cl=u16(head+32);
    uint32_t crc=u32(head+16),compressed=u32(head+20),plain=u32(head+24),loff=u32(head+42);
    uint32_t mode=u32(head+38)>>16;
    if(flags&1||nl==0||nl>=2048||!(method==0||method==8))goto done;
    char name[2048],path[4096];
    if(!read_at(f,pos+46,name,nl))goto done;
    name[nl]=0;
    if(!safe(name)||snprintf(path,sizeof(path),"%s/%s",root,name)>=(int)sizeof(path)){fprintf(stderr,"ZIP: unsafe entry: %s\n",name);goto done;}
    pos+=46u+nl+el+cl;
    if(!read_at(f,loff,local,30)||u32(local)!=0x04034b50)goto done;
    uint64_t data=(uint64_t)loff+30+u16(local+26)+u16(local+28);
    if(data+compressed>(uint64_t)size)goto done;
    unsigned char *bytes=NULL;
    if(inflate_entry(f,data,compressed,plain,method,&bytes)){fprintf(stderr,"ZIP: inflate failed: %s method=%u compressed=%u plain=%u\n",name,method,compressed,plain);goto done;}
    if((uint32_t)crc32(0,bytes,plain)!=crc){free(bytes);goto done;}
    if(dirs(path)){free(bytes);goto done;}
    if((mode&0170000)==0120000){
      /* Symlink target must remain inside SDK tree; reject absolute/traversal. */
      if(!plain||memchr(bytes,0,plain)||!safe_link_target(name,(char*)bytes)||symlink((char*)bytes,path)){fprintf(stderr,"ZIP: symlink rejected: %s -> %s\n",name,bytes);free(bytes);goto done;}
    }else if(name[nl-1]=='/'){
      if(mkdir(path,0700)&&errno!=EEXIST){free(bytes);goto done;}
    }else{
      FILE *out=fopen(path,"wb");if(!out){free(bytes);goto done;}
      int fail=fwrite(bytes,1,plain,out)!=plain;
      if(fclose(out)||fail){free(bytes);goto done;}
      if(chmod(path,mode&0111?0755:0644)){free(bytes);goto done;}
    }
    free(bytes);
  }
  rc=0;
done:
  fclose(f);
  return rc;
}
