/* SPDX-License-Identifier: MIT */
#include "register.h"
#include <vulkan/vulkan_core.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned registrations;

PFN_vkVoidFunction lvp_GetInstanceProcAddr(VkInstance instance,
                                           const char *name) {
  (void)instance;
  (void)name;
  return NULL;
}

int cosmo_wgpu_vulkan_register_embedded(
    const char *name, void *sysv_get_instance_proc_addr) {
  ++registrations;
  return strcmp(name, "Mesa lavapipe test") ||
         sysv_get_instance_proc_addr != (void *)lvp_GetInstanceProcAddr;
}

static int check(int condition, const char *message) {
  if (condition)
    return 0;
  fprintf(stderr, "FAIL: %s\n", message);
  return 1;
}

int main(void) {
  int failures = 0;
  unsetenv("LP_NUM_THREADS");
  failures += check(!cosmo_wgpu_lavapipe_register(),
                    "embedded lavapipe registration succeeds");
  failures += check(getenv("LP_NUM_THREADS") &&
                        !strcmp(getenv("LP_NUM_THREADS"), "2"),
                    "embedded lavapipe defaults to two raster workers");

  setenv("LP_NUM_THREADS", "1", 1);
  failures += check(!cosmo_wgpu_lavapipe_register(),
                    "embedded lavapipe can register with an override");
  failures += check(!strcmp(getenv("LP_NUM_THREADS"), "1"),
                    "caller raster-worker override is preserved");
  failures += check(registrations == 2, "both registrations reached the loader");
  return failures != 0;
}
