


CC = gcc
ZRPC_SRC = $(ZRPC_ROOT)/src

TARGET = zrpc_generate

all: $(TARGET)

zrpc_generate: zrpc_generate.c $(ZRPC_SRC)/zrpc.c $(ZRPC_SRC)/cJSON.c
	$(CC) -o $@ $^ -ldl -I $(ZRPC_SRC)

gen:
	./zrpc_generate ./zrpc_register.json zrpc_method.c zrpc_caller.c

clean:
	rm -rf zrpc_generate zrpc_method.c zrpc_caller.c

