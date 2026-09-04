
// gcc -o zrpc_generate zrpc_generate.c zrpc.c cJSON.c -ldl

#include "zrpc.h"
#include <stdio.h>
#include <string.h>
#include <assert.h>

extern struct zrpc_func *rpc_caller_table;

int _zrpc_method_client(char *clifile) {

    FILE *fp = fopen(clifile, "w");
    if (!fp) {
        printf("clifile: %s open failed\n", clifile);
        return -1;
    }

    fprintf(fp, "#include \"zrpc.h\"\n");
    fprintf(fp, "#include <stdlib.h>\n\n\n");

    struct zrpc_func *func = rpc_caller_table;
    while(func) {
        
        fprintf(fp, "%s ", func->rettype);
        fprintf(fp, "%s", func->method);
        fprintf(fp, "(");

        int i = 0;
        for (i = 0;i < func->count;i ++) {
            fprintf(fp, "%s %s", func->types[i], func->params[i]);

            if (i != (func->count - 1)) {
                fprintf(fp, ", ");
            }
        }

        fprintf(fp, ") { \n\n");

        fprintf(fp, "\tchar *body = zrpc_request_json_encode(%d", func->count);
        for (i = 0;i < func->count;i ++) {
            fprintf(fp, ", %s", func->params[i]);
        }
        fprintf(fp, ");\n");

        fprintf(fp, "\tchar *payload = zrpc_client_session(body);\n");
        fprintf(fp, "\tchar *response = (char *)zrpc_response_json_decode(payload);\n\n");

        if (strcmp(func->rettype, "char *") == 0) {
            fprintf(fp, "\tchar *ret = strdup(response);\n\n");
        } else if (strcmp(func->rettype, "int") == 0) {
            fprintf(fp, "\tint ret = atoi(response);\n\n");
        } else if (strcmp(func->rettype, "float") == 0) {
            fprintf(fp, "\tfloat ret = strtof(response, NULL);\n\n");
        } else if (strcmp(func->rettype, "double") == 0) {
            fprintf(fp, "\tdouble ret = strtod(response, NULL);\n\n");
        } else {
            assert(0);
        }

        fprintf(fp, "\tfree(response);\n");
        fprintf(fp, "\tfree(payload);\n");
        fprintf(fp, "\tfree(body);\n\n");

        fprintf(fp, "\treturn ret;\n");

        fprintf(fp, "}\n\n");

        fflush(fp);

        func = func->next;
    }

    fclose(fp);
    return 0;
}

// avoid having the same method name
int _zrpc_method_server(char *servfile) {

    FILE *fp = fopen(servfile, "w");
    if (!fp) {
        printf("servfile: %s open failed\n", servfile);
        return -1;
    }

    fprintf(fp, "#include \"zrpc.h\"\n");
    fprintf(fp, "#include <stdlib.h>\n\n\n");
    
	struct zrpc_func *func = rpc_caller_table;
// zrpc_server_caller.c
    while(func) {
// zrpc_xxxx
        fprintf(fp, "%s ", func->rettype);
        fprintf(fp, "zrpc_%s", func->method);

        fprintf(fp, "(");

        int i = 0;
        for (i = 0;i < func->count;i ++) {
            fprintf(fp, "%s %s", func->types[i], func->params[i]);

            if (i != (func->count - 1)) {
                fprintf(fp, ", ");
            }
        }
        fprintf(fp, ") { \n\n}\n\n");

// zrpc_method_xxxx
        fprintf(fp, "void ");
        fprintf(fp, "zrpc_method_%s(struct zrpc_task *task, cJSON *params) { \n\n", func->method);

        for (i = 0;i < func->count;i ++) {
            fprintf(fp, "\tcJSON *cjson_%s = cJSON_GetArrayItem(params, %d);\n", func->params[i], i);
            if (strcmp(func->types[i], "char *") == 0) {
                fprintf(fp, "\t%s %s = cjson_%s->valuestring;\n\n", func->types[i], func->params[i], func->params[i]);
            } else if (strcmp(func->types[i], "float") == 0) {
                fprintf(fp, "\t%s %s = cjson_%s->valuedouble;\n\n", func->types[i], func->params[i], func->params[i]);
            } else if (strcmp(func->types[i], "int") == 0 || strcmp(func->types[i], "double") == 0) {
                fprintf(fp, "\t%s %s = cjson_%s->value%s;\n\n", func->types[i], func->params[i], func->params[i], func->types[i]);
            } else {
                assert(0);
            }
             
        }

        if ( strcmp(func->rettype, "float") == 0 || strcmp(func->rettype, "int") == 0 || strcmp(func->rettype, "double") == 0) {
            fprintf(fp, "\ttask->ret = malloc(sizeof(%s));\n", func->rettype);
            fprintf(fp, "\t*(%s *)task->ret = zrpc_%s(", func->rettype, func->method);
        } else {
            fprintf(fp, "\ttask->ret = zrpc_%s(", func->method);
        }

        for (i = 0;i < func->count;i ++) {
            fprintf(fp, "%s", func->params[i]);
            if (i != func->count - 1) {
                fprintf(fp, ", ");
            }
        }
        fprintf(fp, ");\n\n");

        fprintf(fp, "}\n\n");
// zrpc_response_json_encode_xxxx

        fprintf(fp, "char * ");
        fprintf(fp, "zrpc_response_json_encode_%s(struct zrpc_task *task) { \n\n", func->method);

        fprintf(fp, "\tcJSON *root = cJSON_CreateObject(); \n");
        fprintf(fp, "\tcJSON_AddStringToObject(root, \"namespace\", \"zrpc\");\n\n");

        fprintf(fp, "\tcJSON *config = NULL;\n");
        fprintf(fp, "\tcJSON_AddItemToObject(root, \"config\", config = cJSON_CreateObject());\n");
        fprintf(fp, "\tcJSON_AddStringToObject(config, \"method\", task->method);\n\n");

        fprintf(fp, "\tcJSON *result = NULL;\n");
        fprintf(fp, "\tcJSON_AddItemToObject(config, \"result\", result = cJSON_CreateObject());\n");
        if ( strcmp(func->rettype, "float") == 0 || strcmp(func->rettype, "int") == 0 || strcmp(func->rettype, "double") == 0) {
            fprintf(fp, "\tcJSON_AddNumberToObject(result, \"response\", *(%s *)task->ret);\n", func->rettype);
        } else if (strcmp(func->rettype, "char *") == 0) {
            fprintf(fp, "\tcJSON_AddStringToObject(result, \"response\", (%s )task->ret);\n", func->rettype);
        }
        fprintf(fp, "\tcJSON_AddNumberToObject(result, \"callerid\", task->callerid);\n\n");

        fprintf(fp, "\tchar *out = cJSON_Print(root);\n");
        fprintf(fp, "\tcJSON_Delete(root);\n");
        fprintf(fp, "\treturn out;\n\n");

        fprintf(fp, "}\n\n");

        func = func->next;
    }   
    fclose(fp);


    return 0;
}


// ./zrpc_caller_generate zrpc_register.json zrpc_server_caller.c zrpc_client_caller.c
// zrpc_register.json : method json file
// zrpc_server_caller.c :  
// zrpc_client_caller.c :

int main(int argc, char *argv[]) {

    zrpc_caller_register(argv[1]);

    _zrpc_method_server(argv[2]);

    _zrpc_method_client(argv[3]);

    return 0;
}


