


// gcc -o libzrpc_method.so -fPIC -shared zrpc_method.c cJSON.c


#include "zrpc.h"
#include <stdlib.h>
#include <stdio.h>

char *zrpc_sayhello(char *msg, int length) {

    int i = 0;
	for (i = 0; i < length / 2; i++) {
		char temp = msg[i];
		msg[i] = msg[length - i - 1];
		msg[length - i - 1] = temp;
	}

    return msg;
}

void zrpc_method_sayhello(struct zrpc_task *task, cJSON *params) {

    cJSON *cjson_msg = cJSON_GetArrayItem(params, 0);
    cJSON *cjson_length = cJSON_GetArrayItem(params, 1);

    char *msg = cjson_msg->valuestring;
    int length = cjson_length->valueint;

    task->ret = zrpc_sayhello(msg, length);
    printf("zrpc_method_sayhello: %s\n", (char *)task->ret);
}


char *zrpc_response_json_encode_sayhello(struct zrpc_task *task) {

    cJSON *root = cJSON_CreateObject();
	
	cJSON_AddStringToObject(root, "namespace", "zrpc");

	cJSON *config = NULL;
	cJSON_AddItemToObject(root, "config", config = cJSON_CreateObject());
	cJSON_AddStringToObject(config, "method", task->method);

	cJSON *result = NULL;
	cJSON_AddItemToObject(config, "result", result = cJSON_CreateObject());
	cJSON_AddStringToObject(result, "response", (char *)task->ret);
	cJSON_AddNumberToObject(result, "callerid", task->callerid);

    char *out = cJSON_Print(root);
    cJSON_Delete(root);

    return out;
}


int zrpc_add(int a, int b) {

    return a + b;

}


void zrpc_method_add(struct zrpc_task *task, cJSON *params) {

    cJSON *cjson_a = cJSON_GetArrayItem(params, 0);
    cJSON *cjson_b = cJSON_GetArrayItem(params, 1);

    int a = cjson_a->valueint;
    int b = cjson_b->valueint;

    task->ret = malloc(sizeof(int));

    *(int *)task->ret = zrpc_add(a, b);

}

char *zrpc_response_json_encode_add(struct zrpc_task *task) {

    cJSON *root = cJSON_CreateObject();
	
	cJSON_AddStringToObject(root, "namespace", "zrpc");

	cJSON *config = NULL;
	cJSON_AddItemToObject(root, "config", config = cJSON_CreateObject());
	cJSON_AddStringToObject(config, "method", task->method);

	cJSON *result = NULL;
	cJSON_AddItemToObject(config, "result", result = cJSON_CreateObject());
	cJSON_AddNumberToObject(result, "response", *(int *)task->ret);
	cJSON_AddNumberToObject(result, "callerid", task->callerid);

    char *out = cJSON_Print(root);
    cJSON_Delete(root);

    return out;
}


float zrpc_sub(float a, int b) {

    return a - b;

}

void zrpc_method_sub(struct zrpc_task *task, cJSON *params) {

    cJSON *cjson_a = cJSON_GetArrayItem(params, 0);
    cJSON *cjson_b = cJSON_GetArrayItem(params, 1);

    double a = cjson_a->valuedouble;
    double b = cjson_b->valuedouble;

    task->ret = malloc(sizeof(double));
    
    *(double *)task->ret = zrpc_sub(a, b);
}


char *zrpc_response_json_encode_sub(struct zrpc_task *task) {

    cJSON *root = cJSON_CreateObject();
	
	cJSON_AddStringToObject(root, "namespace", "zrpc");

	cJSON *config = NULL;
	cJSON_AddItemToObject(root, "config", config = cJSON_CreateObject());
	cJSON_AddStringToObject(config, "method", task->method);

	cJSON *result = NULL;
	cJSON_AddItemToObject(config, "result", result = cJSON_CreateObject());
	cJSON_AddNumberToObject(result, "response", *(double *)task->ret);
	cJSON_AddNumberToObject(result, "callerid", task->callerid);

    char *out = cJSON_Print(root);
    cJSON_Delete(root);

    return out;
}


///  
double zrpc_mud(double a, double b) {

    return a * b;

}

void zrpc_method_mud(struct zrpc_task *task, cJSON *params) {

    cJSON *cjson_a = cJSON_GetArrayItem(params, 0);
    cJSON *cjson_b = cJSON_GetArrayItem(params, 1);

    double a = cjson_a->valuedouble;
    double b = cjson_b->valuedouble;

    task->ret = malloc(sizeof(double));
    
    *(double *)task->ret = zrpc_mud(a, b);
}

char *zrpc_response_json_encode_mud(struct zrpc_task *task) {

    cJSON *root = cJSON_CreateObject();
	
	cJSON_AddStringToObject(root, "namespace", "zrpc");

	cJSON *config = NULL;
	cJSON_AddItemToObject(root, "config", config = cJSON_CreateObject());
	cJSON_AddStringToObject(config, "method", task->method);

	cJSON *result = NULL;
	cJSON_AddItemToObject(config, "result", result = cJSON_CreateObject());
	cJSON_AddNumberToObject(result, "response", *(double *)task->ret);
	cJSON_AddNumberToObject(result, "callerid", task->callerid);

    char *out = cJSON_Print(root);
    cJSON_Delete(root);

    return out;
}
