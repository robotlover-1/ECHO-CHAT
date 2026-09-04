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
CPPFLAGS += -Iinclude -Intyco -Isrc
LDLIBS   = -lpthread -ldl

BUILD_DIR    := build
TEST_BIN_DIR := tests/bin
HDRS := include/zrpc.h include/zrpc_protocol.h \
        include/zrpc_client.h include/zrpc_server.h \
        ntyco/nty_coroutine.h

# src objects built under -Iinclude only; ntyco objects under -Intyco -Iinclude.
SRCS    := src/zrpc_io.c src/zrpc_frame.c src/zrpc_error.c src/zrpc_json.c \
           src/zrpc_client.c src/zrpc_server.c src/cJSON.c
NYCOSRC := ntyco/nty_coroutine.c ntyco/nty_schedule.c ntyco/nty_epoll.c ntyco/nty_socket.c
OBJS    := $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(SRCS))
NYCOBJS := $(patsubst ntyco/%.c,$(BUILD_DIR)/nty_%.o,$(NYCOSRC))
LIB     := $(BUILD_DIR)/libzrpc.a

TEST_SRCS := tests/test_frame.c tests/test_io.c tests/test_json.c tests/test_unary.c tests/test_stream.c
TEST_PROGS := $(patsubst tests/%.c,$(TEST_BIN_DIR)/%,$(TEST_SRCS))

# Pure-C tests are ASan/UBSan-safe. test_unary drives the NtyCo scheduler, which
# uses ucontext stack switching that ASan does not support (false positives), so
# it runs only on the plain build. NtyCo memory health is covered by the load
# test's fd/RSS canaries instead.
PURE_TESTS := test_frame test_io test_json
ALL_TESTS  := $(PURE_TESTS) test_unary test_stream
ifeq (,$(findstring sanitize,$(CFLAGS)))
RUN_TESTS := $(ALL_TESTS)
else
RUN_TESTS := $(PURE_TESTS)
endif

.PHONY: all test ccli clean

all: $(LIB)

# standalone C unary client CLI (used by zrpc-go integration tests)
ccli: $(TEST_BIN_DIR)/ccli

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(TEST_BIN_DIR):
	mkdir -p $(TEST_BIN_DIR)

$(BUILD_DIR)/%.o: src/%.c $(HDRS) | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/nty_%.o: ntyco/%.c ntyco/nty_coroutine.h | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(LIB): $(OBJS) $(NYCOBJS)
	$(AR) rcs $@ $^

$(TEST_BIN_DIR)/%: tests/%.c $(LIB) | $(TEST_BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< $(LIB) $(LDLIBS) -o $@

test: $(TEST_PROGS)
	@set -e; for t in $(foreach t,$(RUN_TESTS),$(TEST_BIN_DIR)/$(t)); do echo "== $$t =="; ./$$t; done
	@if [ "$(RUN_TESTS)" != "$(ALL_TESTS)" ]; then echo "(sanitizer build: test_unary skipped -- NtyCo/ucontext vs ASan)"; fi

clean:
	rm -rf $(BUILD_DIR) $(TEST_BIN_DIR)
