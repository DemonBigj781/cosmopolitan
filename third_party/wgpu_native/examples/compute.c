/*
 * SPDX-License-Identifier: MIT OR Apache-2.0
 *
 * Headless compute check adapted from gfx-rs/wgpu-native examples/compute
 * at 6aed50955d934ac36049ba8d002034841633ae02.
 * Upstream license texts are retained in ../upstream/LICENSE.*.
 *
 * The WGSL is embedded so the executable does not depend on a working
 * directory or a separate shader file. Runtime checks are not compiled
 * out by NDEBUG.
 */
#define _POSIX_C_SOURCE 200809L
#include "wgpu.h"
#include "vulkan_loader.h"
#include <cosmo.h>
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define EXPECTED_VERSION UINT32_C(0x1d000101)
#define CALLBACK_TIMEOUT_MS UINT64_C(30000)

enum {
  CHECK_OK = 0,
  CHECK_USAGE = 2,
  CHECK_VERSION = 3,
  CHECK_INITIALIZATION = 4,
  CHECK_EXECUTION = 5,
  CHECK_TIMEOUT = 6,
  CHECK_READBACK = 7,
};

static const char shader[] =
    "@group(0) @binding(0)\n"
    "var<storage, read_write> numbers: array<u32>;\n"
    "fn collatz_iterations(input: u32) -> u32 {\n"
    "  var n = input;\n"
    "  var count = 0u;\n"
    "  loop {\n"
    "    if (n <= 1u) { break; }\n"
    "    if (n % 2u == 0u) {\n"
    "      n = n / 2u;\n"
    "    } else {\n"
    "      if (n >= 1431655765u) { return 4294967295u; }\n"
    "      n = 3u * n + 1u;\n"
    "    }\n"
    "    count = count + 1u;\n"
    "  }\n"
    "  return count;\n"
    "}\n"
    "@compute @workgroup_size(1)\n"
    "fn main(@builtin(global_invocation_id) id: vec3<u32>) {\n"
    "  if (id.x < arrayLength(&numbers)) {\n"
    "    numbers[id.x] = collatz_iterations(numbers[id.x]);\n"
    "  }\n"
    "}\n";

static const char raster_shader[] =
    "struct VertexOutput {\n"
    "  @builtin(position) position: vec4<f32>,\n"
    "};\n"
    "@vertex\n"
    "fn vs_main(@builtin(vertex_index) vertex_index: u32) -> VertexOutput {\n"
    "  var positions = array<vec2<f32>, 3>(\n"
    "    vec2<f32>(-0.75, -0.75),\n"
    "    vec2<f32>( 0.75, -0.75),\n"
    "    vec2<f32>( 0.00,  0.75),\n"
    "  );\n"
    "  var output: VertexOutput;\n"
    "  output.position = vec4<f32>(positions[vertex_index], 0.0, 1.0);\n"
    "  return output;\n"
    "}\n"
    "@fragment\n"
    "fn fs_main() -> @location(0) vec4<f32> {\n"
    "  return vec4<f32>(0.9, 0.2, 0.1, 1.0);\n"
    "}\n";

enum Workload {
  WORKLOAD_COLLATZ,
  WORKLOAD_MATRIX,
  WORKLOAD_RASTER,
};

enum {
  RASTER_WIDTH = 64,
  RASTER_HEIGHT = 64,
  RASTER_BYTES_PER_PIXEL = 4,
  RASTER_BYTES_PER_ROW = 256,
  RASTER_BYTES = RASTER_BYTES_PER_ROW * RASTER_HEIGHT,
};

_Static_assert(RASTER_WIDTH * RASTER_BYTES_PER_PIXEL <= RASTER_BYTES_PER_ROW,
               "Raster readback rows must fit their aligned stride");

/* Odd dimensions exercise partial workgroups and the last shared-memory tile. */
enum {
  MATRIX_ROWS = 13,
  MATRIX_INNER = 17,
  MATRIX_COLS = 11,
  MATRIX_A_WORDS = MATRIX_ROWS * MATRIX_INNER,
  MATRIX_B_WORDS = MATRIX_INNER * MATRIX_COLS,
  MATRIX_RESULT_OFFSET = MATRIX_A_WORDS + MATRIX_B_WORDS,
  MATRIX_WORDS = MATRIX_RESULT_OFFSET + MATRIX_ROWS * MATRIX_COLS,
};

static const char matrix_shader[] =
    "@group(0) @binding(0)\n"
    "var<storage, read_write> data: array<f32>;\n"
    "var<workgroup> tile_a: array<f32, 64>;\n"
    "var<workgroup> tile_b: array<f32, 64>;\n"
    "@compute @workgroup_size(8, 8, 1)\n"
    "fn main(@builtin(global_invocation_id) id: vec3<u32>,\n"
    "        @builtin(local_invocation_id) lid: vec3<u32>) {\n"
    "  let row = id.y;\n"
    "  let col = id.x;\n"
    "  let local_index = lid.y * 8u + lid.x;\n"
    "  var sum = 0.0f;\n"
    "  for (var base = 0u; base < 17u; base += 8u) {\n"
    "    tile_a[local_index] = 0.0f;\n"
    "    tile_b[local_index] = 0.0f;\n"
    "    if (row < 13u && base + lid.x < 17u) {\n"
    "      tile_a[local_index] = data[row * 17u + base + lid.x];\n"
    "    }\n"
    "    if (col < 11u && base + lid.y < 17u) {\n"
    "      tile_b[local_index] = data[221u + (base + lid.y) * 11u + col];\n"
    "    }\n"
    "    workgroupBarrier();\n"
    "    for (var k = 0u; k < 8u; k += 1u) {\n"
    "      sum += tile_a[lid.y * 8u + k] * tile_b[k * 8u + lid.x];\n"
    "    }\n"
    "    workgroupBarrier();\n"
    "  }\n"
    "  if (row < 13u && col < 11u) {\n"
    "    data[408u + row * 11u + col] = sum;\n"
    "  }\n"
    "}\n";

_Static_assert(sizeof(float) == sizeof(uint32_t), "The matrix check requires f32");

static void store_float(uint32_t *word, float value) {
  memcpy(word, &value, sizeof(value));
}

static float load_float(const uint32_t *word) {
  float value;
  memcpy(&value, word, sizeof(value));
  return value;
}

static void prepare_matrix(uint32_t *input, uint32_t *expected) {
  for (int row = 0; row < MATRIX_ROWS; ++row)
    for (int k = 0; k < MATRIX_INNER; ++k)
      store_float(&input[row * MATRIX_INNER + k],
                  (float)((row * 3 + k * 5) % 19 - 9) / 8.0f);
  for (int k = 0; k < MATRIX_INNER; ++k)
    for (int col = 0; col < MATRIX_COLS; ++col)
      store_float(&input[MATRIX_A_WORDS + k * MATRIX_COLS + col],
                  (float)((k * 7 + col * 2) % 23 - 11) / 16.0f);
  memcpy(expected, input, MATRIX_WORDS * sizeof(*input));
  for (int row = 0; row < MATRIX_ROWS; ++row) {
    for (int col = 0; col < MATRIX_COLS; ++col) {
      double sum = 0;
      for (int k = 0; k < MATRIX_INNER; ++k)
        sum += (double)load_float(&input[row * MATRIX_INNER + k]) *
               load_float(&input[MATRIX_A_WORDS + k * MATRIX_COLS + col]);
      store_float(&expected[MATRIX_RESULT_OFFSET + row * MATRIX_COLS + col],
                  (float)sum);
    }
  }
}

struct AdapterRequest {
  atomic_int done;
  WGPURequestAdapterStatus status;
  WGPUAdapter adapter;
};

struct DeviceRequest {
  atomic_int done;
  WGPURequestDeviceStatus status;
  WGPUDevice device;
};

struct MapRequest {
  atomic_int done;
  WGPUMapAsyncStatus status;
};

static atomic_int execution_error;
static bool trace_enabled;

static void trace_stage(const char *stage) {
  if (trace_enabled) {
    fprintf(stderr, "compute stage: %s\n", stage);
    fflush(stderr);
  }
}

static void print_view(FILE *stream, WGPUStringView message) {
  if (message.data) {
    size_t length = message.length == WGPU_STRLEN
                        ? strlen(message.data)
                        : message.length;
    fwrite(message.data, 1, length, stream);
  }
}

static void print_error(const char *operation, WGPUStringView message) {
  fprintf(stderr, "%s: ", operation);
  print_view(stderr, message);
  fputc('\n', stderr);
}

static void log_message(WGPULogLevel level, WGPUStringView message,
                        void *userdata) {
  (void)userdata;
  fprintf(stderr, "wgpu log %u: ", (unsigned)level);
  print_view(stderr, message);
  fputc('\n', stderr);
}

static void adapter_ready(WGPURequestAdapterStatus status, WGPUAdapter adapter,
                          WGPUStringView message, void *userdata1,
                          void *userdata2) {
  struct AdapterRequest *request = userdata1;
  (void)userdata2;
  request->status = status;
  request->adapter = adapter;
  if (status != WGPURequestAdapterStatus_Success)
    print_error("request adapter", message);
  atomic_store_explicit(&request->done, 1, memory_order_release);
}

static void device_ready(WGPURequestDeviceStatus status, WGPUDevice device,
                         WGPUStringView message, void *userdata1,
                         void *userdata2) {
  struct DeviceRequest *request = userdata1;
  (void)userdata2;
  request->status = status;
  request->device = device;
  if (status != WGPURequestDeviceStatus_Success)
    print_error("request device", message);
  atomic_store_explicit(&request->done, 1, memory_order_release);
}

static void buffer_mapped(WGPUMapAsyncStatus status, WGPUStringView message,
                          void *userdata1, void *userdata2) {
  struct MapRequest *request = userdata1;
  (void)userdata2;
  request->status = status;
  if (status != WGPUMapAsyncStatus_Success)
    print_error("map readback buffer", message);
  atomic_store_explicit(&request->done, 1, memory_order_release);
}

static void uncaptured_error(WGPUDevice const *device, WGPUErrorType type,
                             WGPUStringView message, void *userdata1,
                             void *userdata2) {
  (void)device;
  (void)userdata1;
  (void)userdata2;
  fprintf(stderr, "WebGPU error %u: ", (unsigned)type);
  print_view(stderr, message);
  fputc('\n', stderr);
  atomic_store_explicit(&execution_error, 1, memory_order_release);
}

static void device_lost(WGPUDevice const *device, WGPUDeviceLostReason reason,
                        WGPUStringView message, void *userdata1,
                        void *userdata2) {
  (void)device;
  (void)userdata1;
  (void)userdata2;
  if (reason != WGPUDeviceLostReason_Destroyed &&
      reason != WGPUDeviceLostReason_CallbackCancelled) {
    print_error("device lost", message);
    atomic_store_explicit(&execution_error, 1, memory_order_release);
  }
}

static int monotonic_ms(uint64_t *value) {
  struct timespec time;
  if (clock_gettime(CLOCK_MONOTONIC, &time)) {
    perror("clock_gettime");
    return CHECK_EXECUTION;
  }
  *value = (uint64_t)time.tv_sec * 1000 + (uint64_t)time.tv_nsec / 1000000;
  return CHECK_OK;
}

static int wait_callback(WGPUInstance instance, WGPUDevice device,
                         atomic_int *done, const char *operation) {
  const struct timespec pause = {0, 1000000};
  uint64_t start, now;
  if (monotonic_ms(&start))
    return CHECK_EXECUTION;
  for (;;) {
    if (atomic_load_explicit(&execution_error, memory_order_acquire))
      return CHECK_EXECUTION;
    if (atomic_load_explicit(done, memory_order_acquire))
      return CHECK_OK;
    if (device)
      (void)wgpuDevicePoll(device, false, NULL);
    else
      wgpuInstanceProcessEvents(instance);
    if (atomic_load_explicit(done, memory_order_acquire))
      continue;
    if (monotonic_ms(&now))
      return CHECK_EXECUTION;
    if (now - start >= CALLBACK_TIMEOUT_MS) {
      fprintf(stderr, "%s: callback timed out after 30 seconds\n", operation);
      return CHECK_TIMEOUT;
    }
    if (nanosleep(&pause, NULL) && errno != EINTR) {
      perror("nanosleep");
      return CHECK_EXECUTION;
    }
  }
}

static const char *adapter_kind(WGPUAdapterType type) {
  switch (type) {
  case WGPUAdapterType_DiscreteGPU:
    return "discrete GPU";
  case WGPUAdapterType_IntegratedGPU:
    return "integrated GPU";
  case WGPUAdapterType_CPU:
    return "CPU/software";
  default:
    return "unknown";
  }
}

static int check_version(void) {
  /* This calls Rust but does not create an instance or load a GPU backend. */
  uint32_t version = wgpuGetVersion();
  printf("wgpu-native %" PRIu32 ".%" PRIu32 ".%" PRIu32 ".%" PRIu32
         " (0x%08" PRIx32 ")\n",
         version >> 24, (version >> 16) & 255, (version >> 8) & 255,
         version & 255, version);
  if (version != EXPECTED_VERSION) {
    fprintf(stderr,
            "version check failed: expected 29.0.1.1 (0x1d000101); build Rust "
            "with WGPU_NATIVE_VERSION=v29.0.1.1\n");
    return CHECK_VERSION;
  }
  puts("C -> Rust version check: PASS");
  return CHECK_OK;
}

#define REQUIRE(condition, code, message)                                      \
  do {                                                                        \
    if (!(condition)) {                                                       \
      fprintf(stderr, "compute check failed: %s\n", message);                  \
      result = code;                                                          \
      goto cleanup;                                                           \
    }                                                                         \
  } while (0)

#define REQUIRE_OBJECT(object, message)                                        \
  REQUIRE((object) &&                                                          \
              !atomic_load_explicit(&execution_error, memory_order_acquire),   \
          CHECK_EXECUTION, message)

static int check_workload(enum Workload workload, bool software) {
  const bool matrix = workload == WORKLOAD_MATRIX;
  const bool raster = workload == WORKLOAD_RASTER;
  uint32_t input[MATRIX_WORDS] = {1, 2, 3, 4};
  uint32_t expected[MATRIX_WORDS] = {0, 1, 7, 2};
  uint32_t output[MATRIX_WORDS] = {0};
  const size_t words = matrix ? MATRIX_WORDS : 4;
  const size_t data_bytes = words * sizeof(uint32_t);
  const size_t readback_bytes = raster ? RASTER_BYTES : data_bytes;
  if (matrix)
    prepare_matrix(input, expected);
  struct AdapterRequest adapter_request = {0};
  struct DeviceRequest device_request = {0};
  struct MapRequest map_request = {0};
  WGPUInstance instance = NULL;
  WGPUAdapter adapter = NULL;
  WGPUDevice device = NULL;
  WGPUQueue queue = NULL;
  WGPUShaderModule module = NULL;
  WGPUBuffer storage = NULL, staging = NULL;
  WGPUComputePipeline pipeline = NULL;
  WGPURenderPipeline render_pipeline = NULL;
  WGPUBindGroupLayout layout = NULL;
  WGPUBindGroup group = NULL;
  WGPUTexture target = NULL;
  WGPUTextureView target_view = NULL;
  WGPUCommandEncoder encoder = NULL;
  WGPUComputePassEncoder pass = NULL;
  WGPURenderPassEncoder render_pass = NULL;
  WGPUCommandBuffer commands = NULL;
  bool mapped = false;
  int result = CHECK_OK;

  /*
   * Avoid driver debug/validation callbacks in this experimental ABI port.
   * These are Vulkan-specific flags, not WebGPU's normal API validation.
   */
  WGPUInstanceExtras extras = {
      .chain = {.sType = (WGPUSType)WGPUSType_InstanceExtras},
      .backends = WGPUInstanceBackend_Vulkan,
      .flags = 0,
  };
  WGPUInstanceDescriptor instance_desc = WGPU_INSTANCE_DESCRIPTOR_INIT;
  instance_desc.nextInChain = &extras.chain;
  WGPURequestAdapterOptions adapter_options = WGPU_REQUEST_ADAPTER_OPTIONS_INIT;
  adapter_options.backendType = WGPUBackendType_Vulkan;
  adapter_options.forceFallbackAdapter = software;
  WGPUDeviceDescriptor device_desc = WGPU_DEVICE_DESCRIPTOR_INIT;
  device_desc.deviceLostCallbackInfo.mode = WGPUCallbackMode_AllowProcessEvents;
  device_desc.deviceLostCallbackInfo.callback = device_lost;
  device_desc.uncapturedErrorCallbackInfo.callback = uncaptured_error;

  wgpuSetLogCallback(log_message, NULL);
  wgpuSetLogLevel(trace_enabled ? WGPULogLevel_Debug : WGPULogLevel_Warn);
  trace_stage("create instance");
  instance = wgpuCreateInstance(&instance_desc);
  REQUIRE(instance, CHECK_INITIALIZATION, "could not create a Vulkan instance");
  trace_stage("request adapter");
  wgpuInstanceRequestAdapter(
      instance, &adapter_options,
      (WGPURequestAdapterCallbackInfo){
          .mode = WGPUCallbackMode_AllowProcessEvents,
          .callback = adapter_ready,
          .userdata1 = &adapter_request,
      });
  result = wait_callback(instance, NULL, &adapter_request.done, "request adapter");
  adapter = adapter_request.adapter;
  if (result)
    goto cleanup;
  REQUIRE(adapter_request.status == WGPURequestAdapterStatus_Success && adapter,
          CHECK_INITIALIZATION, "no suitable Vulkan adapter");

  WGPUAdapterInfo info = WGPU_ADAPTER_INFO_INIT;
  WGPUStatus info_status = wgpuAdapterGetInfo(adapter, &info);
  REQUIRE(info_status == WGPUStatus_Success, CHECK_INITIALIZATION,
          "could not read adapter information");
  fputs("adapter: ", stdout);
  print_view(stdout, info.device);
  fputs("; ", stdout);
  print_view(stdout, info.description);
  printf("; backend=%u (Vulkan=%u); type=%s\n", (unsigned)info.backendType,
         (unsigned)WGPUBackendType_Vulkan, adapter_kind(info.adapterType));
  bool is_vulkan = info.backendType == WGPUBackendType_Vulkan;
  bool is_cpu = info.adapterType == WGPUAdapterType_CPU;
  trace_stage("release adapter information");
  wgpuAdapterInfoFreeMembers(info);
  REQUIRE(is_vulkan, CHECK_INITIALIZATION, "adapter did not use Vulkan");
  REQUIRE(!software || is_cpu, CHECK_INITIALIZATION,
          "embedded software execution did not select a CPU adapter");

  trace_stage("request device");
  wgpuAdapterRequestDevice(
      adapter, &device_desc,
      (WGPURequestDeviceCallbackInfo){
          .mode = WGPUCallbackMode_AllowProcessEvents,
          .callback = device_ready,
          .userdata1 = &device_request,
      });
  trace_stage("wait for device callback");
  result = wait_callback(instance, NULL, &device_request.done, "request device");
  device = device_request.device;
  if (result)
    goto cleanup;
  REQUIRE(device_request.status == WGPURequestDeviceStatus_Success && device,
          CHECK_INITIALIZATION, "could not create a WebGPU device");
  trace_stage("get device queue");
  queue = wgpuDeviceGetQueue(device);
  REQUIRE_OBJECT(queue, "could not obtain queue");

  WGPUShaderSourceWGSL source = WGPU_SHADER_SOURCE_WGSL_INIT;
  source.code = (WGPUStringView){raster ? raster_shader
                                        : matrix ? matrix_shader : shader,
                                 WGPU_STRLEN};
  WGPUShaderModuleDescriptor shader_desc = WGPU_SHADER_MODULE_DESCRIPTOR_INIT;
  shader_desc.nextInChain = &source.chain;
  trace_stage("create WGSL shader module");
  module = wgpuDeviceCreateShaderModule(device, &shader_desc);
  REQUIRE_OBJECT(module, "embedded WGSL compilation failed");

  WGPUBufferDescriptor buffer_desc = WGPU_BUFFER_DESCRIPTOR_INIT;
  buffer_desc.size = readback_bytes;
  buffer_desc.usage = WGPUBufferUsage_MapRead | WGPUBufferUsage_CopyDst;
  trace_stage("create readback buffer");
  staging = wgpuDeviceCreateBuffer(device, &buffer_desc);
  REQUIRE_OBJECT(staging, "could not create readback buffer");

  encoder = wgpuDeviceCreateCommandEncoder(device, NULL);
  REQUIRE_OBJECT(encoder, "could not create command encoder");
  if (raster) {
    WGPUTextureDescriptor texture_desc = WGPU_TEXTURE_DESCRIPTOR_INIT;
    texture_desc.usage =
        WGPUTextureUsage_RenderAttachment | WGPUTextureUsage_CopySrc;
    texture_desc.size = (WGPUExtent3D){RASTER_WIDTH, RASTER_HEIGHT, 1};
    texture_desc.format = WGPUTextureFormat_RGBA8Unorm;
    trace_stage("create raster target");
    target = wgpuDeviceCreateTexture(device, &texture_desc);
    REQUIRE_OBJECT(target, "could not create raster target texture");
    target_view = wgpuTextureCreateView(target, NULL);
    REQUIRE_OBJECT(target_view, "could not create raster target view");

    WGPUColorTargetState color_target = WGPU_COLOR_TARGET_STATE_INIT;
    color_target.format = WGPUTextureFormat_RGBA8Unorm;
    WGPUFragmentState fragment = WGPU_FRAGMENT_STATE_INIT;
    fragment.module = module;
    fragment.entryPoint = (WGPUStringView){"fs_main", WGPU_STRLEN};
    fragment.targetCount = 1;
    fragment.targets = &color_target;
    WGPURenderPipelineDescriptor render_desc =
        WGPU_RENDER_PIPELINE_DESCRIPTOR_INIT;
    render_desc.vertex.module = module;
    render_desc.vertex.entryPoint =
        (WGPUStringView){"vs_main", WGPU_STRLEN};
    render_desc.primitive.topology = WGPUPrimitiveTopology_TriangleList;
    render_desc.fragment = &fragment;
    trace_stage("create raster pipeline");
    render_pipeline = wgpuDeviceCreateRenderPipeline(device, &render_desc);
    REQUIRE_OBJECT(render_pipeline, "could not create raster pipeline");

    WGPURenderPassColorAttachment attachment =
        WGPU_RENDER_PASS_COLOR_ATTACHMENT_INIT;
    attachment.view = target_view;
    attachment.loadOp = WGPULoadOp_Clear;
    attachment.storeOp = WGPUStoreOp_Store;
    attachment.clearValue = (WGPUColor){0.0, 0.0, 0.0, 1.0};
    WGPURenderPassDescriptor pass_desc = WGPU_RENDER_PASS_DESCRIPTOR_INIT;
    pass_desc.colorAttachmentCount = 1;
    pass_desc.colorAttachments = &attachment;
    trace_stage("encode triangle raster pass");
    render_pass = wgpuCommandEncoderBeginRenderPass(encoder, &pass_desc);
    REQUIRE_OBJECT(render_pass, "could not begin raster pass");
    wgpuRenderPassEncoderSetPipeline(render_pass, render_pipeline);
    wgpuRenderPassEncoderDraw(render_pass, 3, 1, 0, 0);
    wgpuRenderPassEncoderEnd(render_pass);
    wgpuRenderPassEncoderRelease(render_pass);
    render_pass = NULL;

    WGPUTexelCopyTextureInfo copy_source =
        WGPU_TEXEL_COPY_TEXTURE_INFO_INIT;
    copy_source.texture = target;
    WGPUTexelCopyBufferInfo copy_destination =
        WGPU_TEXEL_COPY_BUFFER_INFO_INIT;
    copy_destination.buffer = staging;
    copy_destination.layout.bytesPerRow = RASTER_BYTES_PER_ROW;
    copy_destination.layout.rowsPerImage = RASTER_HEIGHT;
    WGPUExtent3D copy_size = {RASTER_WIDTH, RASTER_HEIGHT, 1};
    wgpuCommandEncoderCopyTextureToBuffer(
        encoder, &copy_source, &copy_destination, &copy_size);
  } else {
    buffer_desc.size = data_bytes;
    buffer_desc.usage = WGPUBufferUsage_Storage | WGPUBufferUsage_CopyDst |
                        WGPUBufferUsage_CopySrc;
    trace_stage("create storage buffer");
    storage = wgpuDeviceCreateBuffer(device, &buffer_desc);
    REQUIRE_OBJECT(storage, "could not create storage buffer");

    WGPUComputePipelineDescriptor pipeline_desc =
        WGPU_COMPUTE_PIPELINE_DESCRIPTOR_INIT;
    pipeline_desc.compute.module = module;
    pipeline_desc.compute.entryPoint = (WGPUStringView){"main", WGPU_STRLEN};
    trace_stage("create compute pipeline");
    pipeline = wgpuDeviceCreateComputePipeline(device, &pipeline_desc);
    REQUIRE_OBJECT(pipeline, "could not create compute pipeline");
    trace_stage("create resource bindings");
    layout = wgpuComputePipelineGetBindGroupLayout(pipeline, 0);
    REQUIRE_OBJECT(layout, "could not obtain bind group layout");
    WGPUBindGroupEntry entry = WGPU_BIND_GROUP_ENTRY_INIT;
    entry.binding = 0;
    entry.buffer = storage;
    entry.size = data_bytes;
    WGPUBindGroupDescriptor group_desc = WGPU_BIND_GROUP_DESCRIPTOR_INIT;
    group_desc.layout = layout;
    group_desc.entryCount = 1;
    group_desc.entries = &entry;
    group = wgpuDeviceCreateBindGroup(device, &group_desc);
    REQUIRE_OBJECT(group, "could not create bind group");

    trace_stage("encode compute and readback commands");
    pass = wgpuCommandEncoderBeginComputePass(encoder, NULL);
    REQUIRE_OBJECT(pass, "could not begin compute pass");
    wgpuComputePassEncoderSetPipeline(pass, pipeline);
    wgpuComputePassEncoderSetBindGroup(pass, 0, group, 0, NULL);
    wgpuComputePassEncoderDispatchWorkgroups(pass, matrix ? 2 : 4,
                                             matrix ? 2 : 1, 1);
    wgpuComputePassEncoderEnd(pass);
    wgpuComputePassEncoderRelease(pass);
    pass = NULL;
    wgpuCommandEncoderCopyBufferToBuffer(encoder, storage, 0, staging, 0,
                                         data_bytes);
  }
  commands = wgpuCommandEncoderFinish(encoder, NULL);
  REQUIRE_OBJECT(commands, "could not finish command buffer");
  if (!raster) {
    trace_stage("upload input");
    wgpuQueueWriteBuffer(queue, storage, 0, input, data_bytes);
  }
  trace_stage(raster ? "submit raster commands" : "submit compute commands");
  wgpuQueueSubmit(queue, 1, &commands);
  REQUIRE(!atomic_load_explicit(&execution_error, memory_order_acquire),
          CHECK_EXECUTION, "command submission failed");

  trace_stage("request readback mapping");
  wgpuBufferMapAsync(
      staging, WGPUMapMode_Read, 0, readback_bytes,
      (WGPUBufferMapCallbackInfo){
          .mode = WGPUCallbackMode_AllowProcessEvents,
          .callback = buffer_mapped,
          .userdata1 = &map_request,
      });
  trace_stage("wait for readback mapping");
  result = wait_callback(instance, device, &map_request.done, "map readback");
  if (result)
    goto cleanup;
  REQUIRE(map_request.status == WGPUMapAsyncStatus_Success, CHECK_EXECUTION,
          "mapping the readback buffer failed");
  mapped = true;
  trace_stage("verify mapped output");
  const void *data =
      wgpuBufferGetConstMappedRange(staging, 0, readback_bytes);
  REQUIRE_OBJECT(data, "mapped range was unavailable");
  if (raster) {
    const uint8_t *pixels = data;
    const uint8_t *center =
        pixels + (RASTER_HEIGHT / 2) * RASTER_BYTES_PER_ROW +
        (RASTER_WIDTH / 2) * RASTER_BYTES_PER_PIXEL;
    const unsigned expected_red = 230;
    const unsigned expected_green = 51;
    const unsigned expected_blue = 26;
    size_t covered = 0;
    for (unsigned y = 0; y < RASTER_HEIGHT; ++y) {
      for (unsigned x = 0; x < RASTER_WIDTH; ++x) {
        const uint8_t *pixel = pixels + y * RASTER_BYTES_PER_ROW +
                               x * RASTER_BYTES_PER_PIXEL;
        if (pixel[0] >= 220 && pixel[1] >= 45 && pixel[1] <= 60 &&
            pixel[2] >= 20 && pixel[2] <= 35 && pixel[3] == 255)
          ++covered;
      }
    }
    REQUIRE(center[0] >= expected_red - 2 && center[0] <= expected_red + 2 &&
                center[1] >= expected_green - 2 &&
                center[1] <= expected_green + 2 &&
                center[2] >= expected_blue - 2 &&
                center[2] <= expected_blue + 2 && center[3] == 255,
            CHECK_READBACK, "triangle center pixel has the wrong color");
    const unsigned corners[][2] = {
        {0, 0}, {RASTER_WIDTH - 1, 0},
        {0, RASTER_HEIGHT - 1}, {RASTER_WIDTH - 1, RASTER_HEIGHT - 1},
    };
    for (size_t i = 0; i < sizeof(corners) / sizeof(corners[0]); ++i) {
      const uint8_t *pixel =
          pixels + corners[i][1] * RASTER_BYTES_PER_ROW +
          corners[i][0] * RASTER_BYTES_PER_PIXEL;
      REQUIRE(pixel[0] == 0 && pixel[1] == 0 && pixel[2] == 0 &&
                  pixel[3] == 255,
              CHECK_READBACK, "raster target corner was not clear black");
    }
    REQUIRE(covered >= 800 && covered <= 1400, CHECK_READBACK,
            "triangle coverage was outside the expected range");
    printf("rasterized triangle: 64x64 RGBA8; %zu covered pixels; "
           "center rgba=[%u, %u, %u, %u]; clear corners: PASS\n",
           covered, center[0], center[1], center[2], center[3]);
  } else {
    memcpy(output, data, data_bytes);
  }
  if (!raster && !matrix)
    printf("input: [1, 2, 3, 4]\nreadback: [%" PRIu32 ", %" PRIu32 ", %" PRIu32
           ", %" PRIu32 "]\n",
           output[0], output[1], output[2], output[3]);
  float max_error = 0;
  for (size_t i = 0; !raster && i < words; ++i) {
    if (matrix && i >= MATRIX_RESULT_OFFSET) {
      float want = load_float(&expected[i]);
      float got = load_float(&output[i]);
      float error = fabsf(got - want);
      if (!isfinite(got) || error > 0.00001f + 0.00001f * fabsf(want)) {
        fprintf(stderr, "matrix element %zu: expected %.9g, got %.9g\n",
                i - MATRIX_RESULT_OFFSET, (double)want, (double)got);
        result = CHECK_READBACK;
        goto cleanup;
      }
      max_error = fmaxf(max_error, error);
      continue;
    }
    if (output[i] != expected[i]) {
      fprintf(stderr, "element %zu: expected %" PRIu32 ", got %" PRIu32 "\n",
              i, expected[i], output[i]);
      result = CHECK_READBACK;
      goto cleanup;
    }
  }
  if (matrix)
    printf("f32 matrix multiplication: 13x17 * 17x11; "
           "143 results verified; max error %.9g; "
           "shared memory, barriers, and partial tiles: PASS\n",
           (double)max_error);

cleanup:
  trace_stage("release resources");
  if (mapped)
    wgpuBufferUnmap(staging);
  if (commands)
    wgpuCommandBufferRelease(commands);
  if (render_pass)
    wgpuRenderPassEncoderRelease(render_pass);
  if (pass)
    wgpuComputePassEncoderRelease(pass);
  if (encoder)
    wgpuCommandEncoderRelease(encoder);
  if (group)
    wgpuBindGroupRelease(group);
  if (layout)
    wgpuBindGroupLayoutRelease(layout);
  if (pipeline)
    wgpuComputePipelineRelease(pipeline);
  if (render_pipeline)
    wgpuRenderPipelineRelease(render_pipeline);
  if (staging)
    wgpuBufferRelease(staging);
  if (storage)
    wgpuBufferRelease(storage);
  if (target_view)
    wgpuTextureViewRelease(target_view);
  if (target)
    wgpuTextureRelease(target);
  if (module)
    wgpuShaderModuleRelease(module);
  if (queue)
    wgpuQueueRelease(queue);
  if (device) {
    wgpuDeviceDestroy(device);
    wgpuDeviceRelease(device);
  }
  if (adapter)
    wgpuAdapterRelease(adapter);
  if (instance)
    wgpuInstanceRelease(instance);
  if (result == CHECK_OK &&
      atomic_load_explicit(&execution_error, memory_order_acquire))
    result = CHECK_EXECUTION;
  wgpuSetLogCallback(NULL, NULL);
  if (result == CHECK_OK)
    puts(raster ? "headless Vulkan triangle raster and readback: PASS"
                : "headless Vulkan compute and readback: PASS");
  return result;
}

static void usage(FILE *stream, const char *program) {
  fprintf(stream, "Usage: %s [--software] [--matmul | --raster]\n"
                  "       %s --version | --help\n", program, program);
}

#ifdef COSMO_WGPU_EMBEDDED_LAVAPIPE
/* A strong reference pulls the actual driver out of its static archive. */
int cosmo_wgpu_lavapipe_register(void);
#endif

int main(int argc, char **argv) {
  ShowCrashReports();
  const char *trace = getenv("COSMO_WGPU_TRACE");
  trace_enabled = trace && *trace && strcmp(trace, "0");
  if (argc == 2 && !strcmp(argv[1], "--version"))
    return check_version();
  if (argc == 2 && !strcmp(argv[1], "--help")) {
    usage(stdout, argv[0]);
    puts("Default: check embedded WGSL using the host Vulkan loader and driver.\n"
         "--version: check the linked Rust API without loading a GPU backend.\n"
         "--software: require a linked CPU Vulkan implementation; never load a host driver.\n"
         "--matmul: verify f32 tiled matrix multiplication and workgroup barriers.\n"
         "--raster: draw a headless triangle and verify RGBA8 texture readback.");
    return CHECK_OK;
  }
  enum Workload workload = WORKLOAD_COLLATZ;
  bool software = false;
  for (int i = 1; i < argc; ++i) {
    if (!strcmp(argv[i], "--matmul") && workload == WORKLOAD_COLLATZ)
      workload = WORKLOAD_MATRIX;
    else if (!strcmp(argv[i], "--raster") &&
             workload == WORKLOAD_COLLATZ)
      workload = WORKLOAD_RASTER;
    else if (!strcmp(argv[i], "--software") && !software)
      software = true;
    else {
      usage(stderr, argv[0]);
      return CHECK_USAGE;
    }
  }
  if (software) {
#ifdef COSMO_WGPU_EMBEDDED_LAVAPIPE
    if (cosmo_wgpu_lavapipe_register()) {
      perror("register embedded lavapipe");
      return CHECK_INITIALIZATION;
    }
#endif
    if (cosmo_wgpu_vulkan_select("embedded")) {
      fprintf(stderr, "Embedded software Vulkan is not available in this build.\n"
                      "Rebuild with third_party/wgpu_native/build.sh --software.\n");
      return CHECK_INITIALIZATION;
    }
  }
  printf("Vulkan provider: %s", cosmo_wgpu_vulkan_selected_provider());
  if (software)
    printf(" (%s)", cosmo_wgpu_vulkan_embedded_name());
  putchar('\n');
  int result = check_version();
  if (!result)
    result = check_workload(workload, software);
  unsigned long native_opens = cosmo_wgpu_vulkan_native_open_count();
  printf("native Vulkan loader opens: %lu\n", native_opens);
  if (software && native_opens) {
    fputs("software isolation failed: an OS-native Vulkan loader was opened\n", stderr);
    return CHECK_EXECUTION;
  }
  return result;
}
