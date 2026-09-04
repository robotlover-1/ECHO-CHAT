

// gcc -o zrpc_client zrpc_client.c zrpc.c cJSON.c -ldl -rdynamic
// ./zrpc_client 192.168.243.130 9096 zrpc_register.json


#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <assert.h>

#include "zrpc.h"


char* sayhello (char * msg, int length) {


	char *body = zrpc_request_json_encode(2, msg, length);
	char *payload = zrpc_client_session(body);
	char *response = (char *)zrpc_response_json_decode(payload);

	char *ret = strdup(response);

	free(response);
	free(payload);
	free(body);
	
	return ret;

}

int add (int a, int b) {

	char *body = zrpc_request_json_encode(2, a, b);
	char *payload = zrpc_client_session(body);
	char *response = (char *)zrpc_response_json_decode(payload);

	int ret = atoi(response);
	
	free(response);
	free(payload);
	free(body);

	return ret;
} 

float sub (float a, float b) {

	char *body = zrpc_request_json_encode(2, a, b); 

	char *payload = zrpc_client_session(body);
	char *response = (char *)zrpc_response_json_decode(payload);

	float ret = strtof(response, NULL);

	free(response);
	free(payload);
	free(body);

	return  ret;

} 

double mud(double a, double b) {

	char *body = zrpc_request_json_encode(2, a, b); 

	char *payload = zrpc_client_session(body);
	char *response = (char *)zrpc_response_json_decode(payload);

	double ret = strtod(response, NULL);

	free(response);
	free(payload);
	free(body);
	
	return  ret;
}




int main(int argc, char *argv[]) {

	zrpc_caller_register(argv[1]);

	char *msg = "zrpc nb";	
	char *rpc = sayhello(msg, strlen(msg));

	printf("---> response : %s\n", rpc);
	free(rpc);

	int a = 2;
	int b = 4;

	printf("add: %d\n", add(a, b));

	double m = 6.7;
	double n = 3.4;
	printf("mud: %f\n", mud(m, n));

	
	float x = 4.5;
	float y = 3.4;
	printf("sub: %f\n", sub(x, y));

	return 0;
}


