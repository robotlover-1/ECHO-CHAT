

#ifndef __ZRPC_H__
#define __ZRPC_H__


#include "cJSON.h"

#define ZRPC_PARAMS_MAX_COUNT		16
#define ZRPC_MSG_HEADER_LENGTH		6
#define ZRPC_CRC32_TABLE_LENGTH		256


struct zrpc_task {
    char *method;
    unsigned int callerid;
    void *ret;
};

typedef void (*zrpc_method_handler)(struct zrpc_task *task, cJSON *params);
typedef char *(*zrpc_response_encoder_handler)(struct zrpc_task *task);
typedef void (*zrpc_request_encoder_handler)(int numargs, ...);


struct zrpc_func {
	char *method;
	char *params[ZRPC_PARAMS_MAX_COUNT];
	char *types[ZRPC_PARAMS_MAX_COUNT];
    int count;
	char *rettype;

    zrpc_method_handler handler;
    zrpc_response_encoder_handler response_encoder_handler;


	struct zrpc_func *next;
};

void *zrpc_malloc(size_t size);
void zrpc_free(void *ptr);

char *zrpc_request_json_decode(char *json);
char *zrpc_request_json_encode(int numargs, ...);
void *zrpc_response_json_decode(char *json);

int check_packet(unsigned char *payload, int length, unsigned int crc);
char *zrpc_header_encode(char *msg);

int zrpc_caller_register(char *filename);

void iterator_func(void);

char *zrpc_client_session(char *body);


#endif 

