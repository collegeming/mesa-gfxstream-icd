# inputgrab - host unit tests and Android cross build.
#
# Host:    make test
# Android: make android ANDROID_CC=$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android33-clang

CC ?= cc
CFLAGS ?= -O2 -g -std=c11 -Wall -Wextra
OUT ?= inputgrab

CORE = inputgrab_core.c inputgrab_core.h

.PHONY: all test android clean

all: inputgrab

test: inputgrab_test
	./inputgrab_test

inputgrab_test: inputgrab_test.c $(CORE)
	$(CC) $(CFLAGS) -o $@ inputgrab_test.c inputgrab_core.c

inputgrab: inputgrab.c $(CORE)
	$(CC) $(CFLAGS) -o $@ inputgrab.c inputgrab_core.c

android: inputgrab.c $(CORE)
	$(ANDROID_CC) $(CFLAGS) -fPIE -static-pie -o $(OUT) inputgrab.c inputgrab_core.c

clean:
	rm -f inputgrab_test inputgrab
