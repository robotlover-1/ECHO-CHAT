

#define _GNU_SOURCE
#include <dlfcn.h>

#include "zrpc.h"

#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include <assert.h>
#include <errno.h>
#include <stdarg.h>


static unsigned int crc32_table[ZRPC_CRC32_TABLE_LENGTH] = {0};

struct zrpc_func *rpc_caller_table = NULL;

char *zrpc_server_ip;
unsigned short zrpc_server_port;

static void __attribute__((constructor(1000))) init_crc32_table() {
    unsigned int crc;

    for (int i = 0; i < ZRPC_CRC32_TABLE_LENGTH; ++i) {
        crc = i;
        for (int j = 0; j < 8; ++j) {
            if (crc & 1)
                crc = (crc >> 1) ^ 0xEDB88320;
            else
                crc >>= 1;
        }
        crc32_table[i] = crc;
    }
}

static unsigned int calc_crc32(const unsigned char* data, size_t length) {
    unsigned int crc = 0xFFFFFFFF;
    for (size_t i = 0; i < length; ++i) {
        crc = (crc >> 8) ^ crc32_table[(crc ^ data[i]) & 0xFF];
    }
    return ~crc;
}

int check_packet(unsigned char *payload, int length, unsigned int crc) {

	unsigned checksum = calc_crc32(payload, length);

	if (checksum != crc) {
		return 1;
	}
	return 0;
}


char *zrpc_header_encode(char *msg) {

    char *header = malloc(ZRPC_MSG_HEADER_LENGTH);

    unsigned int crc32 = calc_crc32(msg, strlen(msg));
    *(unsigned int*)header = crc32;
	*(unsigned short*)(header+4) = (unsigned short)strlen(msg);

    return header;
}


// request decode
char *zrpc_request_json_decode(char *json) {

    cJSON *root = NULL;

	root = cJSON_Parse(json);
	if (root == NULL) {
		return NULL;
	}
    cJSON *config = cJSON_GetObjectItem(root, "config");
	cJSON *method = cJSON_GetObjectItem(config, "method");
    cJSON *params = cJSON_GetObjectItem(config, "params");
    cJSON *callerid = cJSON_GetObjectItem(config, "callerid");

    struct zrpc_func *func = rpc_caller_table;
    while (func) {
        
        if (strcmp(func->method, method->valuestring)) {
            func = func->next;
            continue;
        }

        struct zrpc_task *task = malloc(sizeof(struct zrpc_task));
        memset(task, 0, sizeof(struct zrpc_task));

        task->method = method->valuestring;
        task->callerid = callerid->valueint;
        
        func->handler(task, params);  
        char *response = func->response_encoder_handler(task);

        free(task);

        return response;
    }

    return NULL;
}

const char * zrpc_caller_name()
{
    void* return_addr = __builtin_return_address(1);

    Dl_info info;
    if (dladdr(return_addr, &info) != 0 && info.dli_sname != NULL) {
        return info.dli_sname;
    } 

    return NULL;
}

void iterator_func(void) {
    struct zrpc_func *func = rpc_caller_table;

    while (func) {

        printf("methd: %s\n", func->method);
        int i = 0;
        for (i = 0;i < func->count;i ++) {
            printf("type: %s, params: %s\n", func->types[i], func->params[i]);
        }

        func = func->next;
    }
}

char *zrpc_request_json_encode(int numargs, ...) {

    cJSON *root = NULL;
	cJSON *config = NULL;
	cJSON *params = NULL;

    root = cJSON_CreateObject();
	cJSON_AddStringToObject(root, "namespace", "zrpc");

    const char *method = zrpc_caller_name();
    cJSON_AddItemToObject(root, "config", config = cJSON_CreateObject());
	cJSON_AddStringToObject(config, "method", method);

    cJSON_AddItemToObject(config, "params", params = cJSON_CreateObject());
    struct zrpc_func *func = rpc_caller_table;

    while (func) {
        if (0 == strcmp(func->method, method)) {
            break;
        }
        func = func->next;
    }

    if (func == NULL) {
        return NULL;
    }

    assert(numargs == func->count);

    va_list args;
    va_start(args, numargs);

    int i = 0;
    for (i = 0;i < func->count;i ++) {
        
        if (strcmp(func->types[i], "int") == 0) {
            cJSON_AddNumberToObject(params, func->params[i], va_arg(args, int));
        } else if (strcmp(func->types[i], "float") == 0) {  // float must be double
            cJSON_AddNumberToObject(params, func->params[i], va_arg(args, double));
        } else if (strcmp(func->types[i], "double") == 0) {
            cJSON_AddNumberToObject(params, func->params[i], va_arg(args, double));
        } else if (strcmp(func->types[i], "char *") == 0) {
            cJSON_AddStringToObject(params, func->params[i], va_arg(args, char *));
        } else {
            printf("type: %s, value: %d\n", func->types[i], va_arg(args, int));
        }
        //cJSON_AddStringToObject(params, func->params[i], );
    }
    va_end(args);

    cJSON_AddNumberToObject(config, "callerid", 123456); // inc


    char *out = cJSON_Print(root);
    cJSON_Delete(root);

    return out;
}


void *zrpc_response_json_decode(char *json) {

    cJSON *root = NULL;
    root = cJSON_Parse(json);
	if (root == NULL) {
		printf("parse failed\n");
		return NULL;
	}

    cJSON *namespace = cJSON_GetObjectItem(root, "namespace");
    cJSON *config = cJSON_GetObjectItem(root, "config");
    cJSON *method = cJSON_GetObjectItem(config, "method");

    cJSON *result = cJSON_GetObjectItem(config, "result");
    cJSON *response = cJSON_GetObjectItem(result, "response");

    return cJSON_Print(response);
}

void *zrpc_malloc(size_t size) {

    return malloc(size);
}


void zrpc_free(void *ptr) {

    return free(ptr);
}


#define ZRPC_METHOD_LIBSO   "./libzrpc_method.so"
void *dlhandle = NULL;

static void* zrpc_method_find(char *funcname) {

    if (dlhandle == NULL) {
        dlhandle = dlopen(ZRPC_METHOD_LIBSO, RTLD_LAZY);
        if (!dlhandle) {
            return NULL;
        }
    }
    

    void *function = dlsym(dlhandle, funcname);
    if (function == NULL) {
        return NULL;
    }

    //dlclose(handle);

    return function;
}

char *read_conf(char *filename) {

	off_t file_size, bytes_read;

	int fd = open(filename, O_RDONLY);
    if (fd == -1) {
        printf("open failed\n");
        return NULL;
    }
	file_size = lseek(fd, 0, SEEK_END);
	lseek(fd, 0, SEEK_SET);

	char *buffer = (char *)malloc(file_size + 1);
	if (buffer == NULL) {
		printf("malloc failed\n");
		close(fd);
		return NULL;
	}

	bytes_read = read(fd, buffer, file_size);
	buffer[bytes_read] = '\0';

	close(fd);

	return buffer;
}

int zrpc_decode_conf(char *json) {

    cJSON *cjson = NULL;
	cJSON *cjson_namespace = NULL;
    cJSON *cjson_remote = NULL;
    cJSON *cjson_port = NULL;
	cJSON *cjson_config = NULL;
	cJSON *cjson_method = NULL;
	cJSON *cjson_params = NULL;
	cJSON *cjson_types = NULL;
	cJSON *cjson_rettype = NULL;
	cJSON *cjson_func = NULL;

    cjson = cJSON_Parse(json);

	cjson_namespace = cJSON_GetObjectItem(cjson, "namespace");

    cjson_remote = cJSON_GetObjectItem(cjson, "remote");
    zrpc_server_ip = cJSON_GetStringValue(cjson_remote);

    cjson_port = cJSON_GetObjectItem(cjson, "port");
    zrpc_server_port = cjson_port->valueint;

	cjson_config = cJSON_GetObjectItem(cjson, "config");
	int config_size = cJSON_GetArraySize(cjson_config);

    int i = 0;
	for (i = 0;i < config_size;i ++) {

        struct zrpc_func *func = (struct zrpc_func *)malloc(sizeof(struct zrpc_func));
        memset(func, 0, sizeof(struct zrpc_func));

		cjson_func = cJSON_GetArrayItem(cjson_config, i);

		cjson_rettype = cJSON_GetObjectItem(cjson_func, "rettype");
        func->rettype = cjson_rettype->valuestring;

		cjson_method = cJSON_GetObjectItem(cjson_func, "method");
        func->method = cjson_method->valuestring;
        
        char *methodname = malloc(strlen("zrpc_method_") + strlen(func->method) + 1);
        memset(methodname, 0, strlen("zrpc_method_") + strlen(func->method) + 1);
        sprintf(methodname, "zrpc_method_%s", func->method);
        zrpc_method_handler handler = (zrpc_method_handler)zrpc_method_find(methodname);
        func->handler = handler;

        free(methodname);

        methodname = malloc(strlen("zrpc_response_json_encode_") + strlen(func->method) + 1);
        memset(methodname, 0, strlen("zrpc_response_json_encode_") + strlen(func->method) + 1);
        sprintf(methodname, "zrpc_response_json_encode_%s", func->method);
        zrpc_response_encoder_handler res_en_handler = (zrpc_response_encoder_handler)zrpc_method_find(methodname);
        func->response_encoder_handler = res_en_handler;
        free(methodname);
        
		cjson_params = cJSON_GetObjectItem(cjson_func, "params");
		int params_size = cJSON_GetArraySize(cjson_params);

		cjson_types = cJSON_GetObjectItem(cjson_func, "types");
		int type_size = cJSON_GetArraySize(cjson_types);

		assert(params_size == type_size && params_size <= ZRPC_PARAMS_MAX_COUNT);

		int j = 0;
		cJSON *param = NULL;
		cJSON *type = NULL;
		for (j = 0;j < params_size;j ++) {
			param = cJSON_GetArrayItem(cjson_params, j);
            func->params[j] = param->valuestring;

			type = cJSON_GetArrayItem(cjson_types, j);
            func->types[j] = type->valuestring;

		}
        func->count = params_size;

        // list head insert
        func->next = rpc_caller_table;
        rpc_caller_table = func;
	}

    return 0;
}


int zrpc_caller_register(char *filename) {
	
	char *conf = read_conf(filename);
	int ret = zrpc_decode_conf(conf);
    free(conf);

    return ret;
}



int connect_tcpserver(const char *ip, unsigned short port) {	

	int connfd = socket(AF_INET, SOCK_STREAM, 0);	
	
	struct sockaddr_in server_addr;	
	memset(&server_addr, 0, sizeof(struct sockaddr_in));	
	server_addr.sin_family = AF_INET;	
	server_addr.sin_addr.s_addr = inet_addr(ip);	
	server_addr.sin_port = htons(port);	

	if (0 !=  connect(connfd, (struct sockaddr*)&server_addr, sizeof(struct sockaddr_in))) {		
		perror("connect");		
		return -1;	
	}		

	return connfd;	
}


char *zrpc_client_session(char *body) {

    char *header = zrpc_header_encode(body);

	int connfd = connect_tcpserver(zrpc_server_ip, zrpc_server_port);

	ssize_t totalBytesSent = 0;
	totalBytesSent = send(connfd, header, ZRPC_MSG_HEADER_LENGTH, 0);
	totalBytesSent += send(connfd, body, strlen(body), 0);

    memset(header, 0, ZRPC_MSG_HEADER_LENGTH);
    int ret = recv(connfd, header, ZRPC_MSG_HEADER_LENGTH, 0);
	if (ret == 0) {
        free(header);
		close(connfd);
		return NULL;
	}

    unsigned int crc32 = *(int *)header;
	unsigned short reslen = *(unsigned short*)(header+4);
    free(header);

    char *payload = (char *)malloc((reslen + 2) * sizeof(char));
	if (!payload) return NULL;
	memset(payload, 0, (reslen + 2));

    ret = recv(connfd, payload, reslen, 0);
    if (check_packet(payload, reslen, crc32)) {
		printf("check sum failed\n");
        free(payload);
		return NULL;
	}

    close(connfd);

    return payload;
}
