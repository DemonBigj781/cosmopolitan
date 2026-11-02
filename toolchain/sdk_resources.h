#ifndef TOOLCHAIN_SDK_RESOURCES_H_
#define TOOLCHAIN_SDK_RESOURCES_H_

enum SdkComponent {
  SDK_C,
  SDK_RUST,
  SDK_RUST_APE,
  SDK_NODEJS,
  SDK_WEBGPU,
  SDK_ALL
};

int extract_sdk_zip(const char *archive, const char *root,
                    enum SdkComponent component, const char *rust_host);
int embedded_sdk_main(int argc, char **argv);
int embedded_sdk_cleanup(void);
int embedded_sdk_materialize(const char *self, enum SdkComponent component);
const char *embedded_sdk_root(void);
int embedded_sdk_run_script(char *const argv[]);

#endif
