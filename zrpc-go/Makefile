# zrpc-go: cgo bridge over libzrpc.a
#
#   make            build ../third_party/zrpc then `go build ./...`
#   make test       build lib then `CGO_ENABLED=1 go test -race ./...`
#   make vet        go vet

CC_BACKEND = $(MAKE) -C ../third_party/zrpc

.PHONY: all lib ccli build test vet clean

all: build

lib:
	$(CC_BACKEND)

ccli:
	$(CC_BACKEND) ccli

build: lib
	CGO_ENABLED=1 go build ./...

test: lib ccli
	CGO_ENABLED=1 go test -race ./...

vet: lib
	CGO_ENABLED=1 go vet ./...

clean:
	rm -f coverage.out
