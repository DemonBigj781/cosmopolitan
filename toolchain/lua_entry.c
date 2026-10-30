/* Expose the existing Cosmopolitan Lua interpreter through the unified APE. */
#define main lua_entry
#include "third_party/lua/lua.main.c"
#undef main
