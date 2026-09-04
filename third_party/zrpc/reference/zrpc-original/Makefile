
ZRPC_ROOT := $(shell pwd)
NTYCO_ROOT ?= $(ZRPC_ROOT)/NtyCo

ECHO = echo
SUBDIR := $(NTYCO_ROOT) src/ tools/


export ZRPC_ROOT

.PHONY: all clean

all: $(SUBDIR)
$(SUBDIR): ECHO
	make -C $@

ECHO:
	@echo $(SUBDIR)

clean: 
	$(MAKE) -C src/ clean
	$(MAKE) -C tools/ clean
	
