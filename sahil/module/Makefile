# Makefile for jbd2_trace kernel module
# Build: make
# Clean: make clean

obj-m += jbd2_trace.o

KDIR := /lib/modules/$(shell uname -r)/build
PWD := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

# Quick reference:
#   sudo insmod jbd2_trace.ko                      # load with defaults
#   sudo insmod jbd2_trace.ko verbose=0             # summary-only (less dmesg noise)
#   sudo insmod jbd2_trace.ko trace_comm=fsync_bench # trace different process
#   dmesg -w | grep jbd2_trace                      # watch live
#   sudo rmmod jbd2_trace                            # unload → prints summary
