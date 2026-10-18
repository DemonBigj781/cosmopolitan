#ifndef TOOLCHAIN_SDK_RESOURCES_H_
#define TOOLCHAIN_SDK_RESOURCES_H_

enum SdkComponent { SDK_C, SDK_RUST, SDK_RUST_APE, SDK_ALL };

int extract_sdk_zip(const char *archive, const char *root,
                    enum SdkComponent component, const char *rust_host);
int embedded_sdk_main(int argc, char **argv);
int embedded_sdk_cleanup(void);

#endif
