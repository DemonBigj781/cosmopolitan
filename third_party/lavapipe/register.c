/* SPDX-License-Identifier: MIT */
#include <vulkan/vulkan_core.h>
#include <stdlib.h>
#include "register.h"
#include "../wgpu_native/runtime/vulkan_loader.h"

#ifndef COSMO_LAVAPIPE_MESA_VERSION
#error "The pinned Mesa version must be supplied by build.py"
#endif

/* This is Mesa's direct ICD entry, with no system Vulkan loader in between. */
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL
lvp_GetInstanceProcAddr(VkInstance instance, const char *name);

int cosmo_wgpu_lavapipe_register(void) {
  /* Cosmopolitan's current llvmpipe port stalls with three or more raster
     workers. Keep the known-good two-worker default without overriding an
     explicit application choice. */
  if (setenv("LP_NUM_THREADS", "2", 0))
    return -1;
  return cosmo_wgpu_vulkan_register_embedded(
      "Mesa lavapipe " COSMO_LAVAPIPE_MESA_VERSION,
      (void *)lvp_GetInstanceProcAddr);
}
