o/$(MODE)/toolchain/compiler.o \
o/$(MODE)/toolchain/machin_demo.o: private CFLAGS += \
	-std=c11 \
	-pthread \
	-Dmain=machin_entry \
	-Wno-misleading-indentation \
	-Wno-unused-variable

o/aarch64/toolchain/compiler.c: o//toolchain/compiler.c
	@mkdir -p $(@D)
	@cp $< $@

o/aarch64/toolchain/machin_demo.c: o//toolchain/machin_demo.c
	@mkdir -p $(@D)
	@cp $< $@
