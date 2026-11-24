CC ?= gcc
all: libmallinfo_clamp.so mi_big
libmallinfo_clamp.so: mallinfo_clamp.c
	$(CC) -O2 -Wall -shared -fPIC -o $@ $<
mi_big: mi_big.c
	$(CC) -O2 -o $@ $< -lpthread
test: all
	@echo "--- glibc mallinfo() (expect OVERFLOW)"; ./mi_big
	@echo "--- with clamp shim (expect ok)"; LD_PRELOAD=$(CURDIR)/libmallinfo_clamp.so ./mi_big
clean:
	rm -f libmallinfo_clamp.so mi_big
.PHONY: all test clean
