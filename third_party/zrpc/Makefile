# zrpc v2 C library - build libzrpc.a and run C unit tests.
#
# Examples:
#   make                     build build/libzrpc.a
#   make test                build and run unit tests (plain)
#   make clean
#   CFLAGS="-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer" \
#     make -C third_party/zrpc clean test     # sanitizer pass
#
# Note: rebuild with clean when switching CFLAGS (e.g. plain <-> sanitizer) so
# every object is compiled with the same flags.

CC      ?= cc
AR      ?= ar
CFLAGS  ?= -O2 -g
CFLAGS  += -Wall -Wextra
CPPFLAGS += -Iinclude
LDLIBS   = -lpthread

BUILD_DIR    := build
TEST_BIN_DIR := tests/bin
HDRS := include/zrpc.h include/zrpc_protocol.h

SRCS := src/zrpc_io.c src/zrpc_frame.c src/zrpc_error.c
OBJS := $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(SRCS))
LIB  := $(BUILD_DIR)/libzrpc.a

TEST_SRCS := tests/test_frame.c tests/test_io.c
TEST_PROGS := $(patsubst tests/%.c,$(TEST_BIN_DIR)/%,$(TEST_SRCS))

.PHONY: all test clean

all: $(LIB)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(TEST_BIN_DIR):
	mkdir -p $(TEST_BIN_DIR)

$(BUILD_DIR)/%.o: src/%.c $(HDRS) | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(LIB): $(OBJS)
	$(AR) rcs $@ $^

$(TEST_BIN_DIR)/%: tests/%.c $(LIB) | $(TEST_BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< $(LIB) $(LDLIBS) -o $@

test: $(TEST_PROGS)
	@set -e; for t in $(TEST_PROGS); do echo "== $$t =="; ./$$t; done

clean:
	rm -rf $(BUILD_DIR) $(TEST_BIN_DIR)
