o/$(MODE)/toolchain/compiler.o: private CFLAGS += \
	-std=c11 \
	-pthread \
	-Dmain=machin_entry \
	-Wno-misleading-indentation \
	-Wno-unused-variable
