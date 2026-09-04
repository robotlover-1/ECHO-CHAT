


### build 
```
# git clone http://gitlab.0voice.com/King/zrpc.git

# git submodule update --init --recursive

# make

# ./zrpc_server ./zrpc_register.json

# ./zrpc_client ./zrpc_register.json

```

### rpc --> register

```
{
    "namespace": "zrpc",
    "config": [
        {
            "method": "sayhello",
            "params": [ "msg", "length"],
            "type": ["char *", "int"],
            "rettype": "int"
        },
        {
            "method": "add",
            "params": [ "a", "b"],
            "type": ["int", "int"],
            "rettype": "int"
        },
        {
            "method": "sub",
            "params": [ "a", "b"],
            "type": ["float", "float"],
            "rettype": "int"
        },
    ]
}
```

### rpc --> request
```
{
    "namespace": "zrpc",
    "config": [
        {
            "method": "sayhello",
            "params": {
                "msg": "hello zrpc",
                "length": 7
            },
            "callerid": 123456
        },
        {
            "method": "add",
            "params": {
                "a": 2,
                "b": 3
            },
            "callerid": 123457
        },
        {
            "method": "sub",
            "params": {
                "a": 5.7,
                "b": 3.2
            },
            "callerid": 123458
        }
    ]
}
```

### rpc --> response
```
{
    "namespace": "zrpc",
    "config": [
        {
            "method": "sayhello",
            "callerid": 123456
            "result": {
                "response": "Yes"
            }
        },
        {
            "method": "add",
            "callerid": 123457
            "result": {
                "response": 5
            }
        },
        {
            "method": "sub",
            "result": {
                "response": 2.5
            },
            "callerid": 123458
        }
    ]
}
```


### rpc caller table

#### 1. generate caller.c

#### 2. build caller table

#### 3. rpc request

#### 4. rpc response


