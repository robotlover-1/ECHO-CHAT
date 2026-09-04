
CC = gcc

NTYCO_ROOT ?= $(ZRPC_ROOT)/NtyCo

LIBS = -L $(NTYCO_ROOT) -lntyco -lpthread
INCS = -I $(NTYCO_ROOT)/core/



TARGET = zrpc_client zrpc_server libzrpc_method.so 

all: $(TARGET)

zrpc_server: zrpc_server.c zrpc.c cJSON.c
	$(CC) -o $@ $^ $(LIBS) $(INCS) -ldl -rdynamic

zrpc_client: zrpc_client.c zrpc.c cJSON.c
	$(CC) -o $@ $^ -ldl -rdynamic

libzrpc_method.so: zrpc_method.c cJSON.c
	$(CC) -o $@ $^ -fPIC -shared

clean:
	rm -rf *.o *.so zrpc_server zrpc_client

