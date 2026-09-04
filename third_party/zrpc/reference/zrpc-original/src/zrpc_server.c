

// gcc -o zrpc_server zrpc_server.c zrpc.c cJSON.c -I /home/king/share/rpc/NtyCo/core -L /home/king/share/rpc/NtyCo/ -lntyco -ldl -lpthread

#include "zrpc.h"
#include "nty_coroutine.h"
#include <arpa/inet.h>

// char *

char* zrpc_process(char *json) {

	//zrpc_request_json_decode_sayhello();

	return zrpc_request_json_decode(json);

}


void zrpc_handle(void *arg) {

	int clientfd = *(int *)arg;
	free(arg);

	while (1) {

		char header[ZRPC_MSG_HEADER_LENGTH] = {0};
		int ret = recv(clientfd, header, ZRPC_MSG_HEADER_LENGTH, 0);
		if (ret == 0) {
			close(clientfd);
			// destory mempool
			
			break;
		}

		unsigned int crc32 = *(int *)header;
		unsigned short length = *(unsigned short*)(header+4);

		char *payload = (char *)zrpc_malloc((length + 2) * sizeof(char));
		if (!payload) continue;
		memset(payload, 0, (length + 2));

		ret = recv(clientfd, payload, length, 0);
		assert(ret == length);
		printf("body: %s\n", payload);

		if (check_packet(payload, length, crc32)) {
			printf("check sum failed\n");
			continue;
		}

		char *response = zrpc_process(payload);
		char *outheader = zrpc_header_encode(response);

		ret = send(clientfd, outheader, ZRPC_MSG_HEADER_LENGTH, 0);
		ret = send(clientfd, response, strlen(response), 0);

		free(response);
		free(outheader);
		free(payload);

	}

}

void zrpc_listen(void *arg) {

	unsigned short port = *(unsigned short *)arg;

	int fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0) return ;

	struct sockaddr_in local, remote;
	local.sin_family = AF_INET;
	local.sin_port = htons(port);
	local.sin_addr.s_addr = INADDR_ANY;
	bind(fd, (struct sockaddr*)&local, sizeof(struct sockaddr_in));

	listen(fd, 20);

	while (1) {

		socklen_t len = sizeof(struct sockaddr_in);
		int cli_fd = accept(fd, (struct sockaddr*)&remote, &len);

		nty_coroutine *handle;
		int *clientfd = (int *)malloc(sizeof(int));
		memcpy(clientfd, &cli_fd, sizeof(int));
		
		nty_coroutine_create(&handle, zrpc_handle, clientfd);

	}

}


int main(int argc, char *argv[]) {

	if (argc != 2) return -1;

	unsigned short port = 9096;

	
	zrpc_caller_register(argv[1]);
	
	nty_coroutine *co = NULL;
	nty_coroutine_create(&co, zrpc_listen, &port);

	nty_schedule_run();

	return 0;

}




