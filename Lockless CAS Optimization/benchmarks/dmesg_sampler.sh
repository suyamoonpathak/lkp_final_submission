#!/bin/bash
set -e

# Prompt User for Hardware Topology
read -p "Enter Target Device [default: /dev/nvme0n1p7]: " DEV
DEV=${DEV:-/dev/nvme0n1p7}

read -p "Enter Mount Path [default: /mnt/analysis_2]: " MNT
MNT=${MNT:-/mnt/analysis_2}

MODULE_DIR=$(pwd)
LOG_DIR="$MODULE_DIR/artifact_ans/dmesg_logs"

sudo mkdir -p $LOG_DIR


for run in 1 2 3 4; do
    echo "=========================================================="
    echo "Running Dmesg Latency Sample $run / 4"
    echo "=========================================================="
    
    # 1. Clear Cache & Wipe Directory completely
    echo "[*] Flushing data, clearing OS pagecache, and wiping mount..."
    sync
    sudo umount $MNT 2>/dev/null || true
    sudo mount -o data=ordered $DEV $MNT
    sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
    sudo rm -rf $MNT/*

    # 2. Clear previous dmesg logs and load module
    echo "[*] Loading module for profiling..."
    sudo rmmod jbd2_trace 2>/dev/null || true
    sudo dmesg -c > /dev/null # Flushes current dmesg so our log is perfectly clean
    DEV_NAME=${DEV#/dev/}
    sudo insmod $MODULE_DIR/jbd2_trace.ko optimize=0 device=$DEV_NAME

    # 3. Run specific FIO config
    echo "[*] Running your Fio target workload..."
    sudo fio --name=ordered_test --directory=$MNT --rw=write --bs=4k --size=1M --fsync=1 --numjobs=8 > /dev/null

    # 4. Remove module safely (forces exit_handler to print the table to dmesg)
    echo "[*] Unloading module to trigger the dmesg summary dump..."
    sudo rmmod jbd2_trace

    # 5. Extract specifically our table from dmesg and save it
    echo "[*] Saving clean dmesg output to dmesg_sample_$run.txt"
    sudo dmesg | grep "jbd2_trace:" > $LOG_DIR/dmesg_sample_$run.txt
    
    echo "Sample $run completed successfully."
    
    if [ "$run" -lt 4 ]; then
        echo "[*] Cooling down hardware for 5 seconds before the next iteration..."
        sleep 5
    fi
    echo ""
done

echo "Done! All 4 kernel log samples are securely gathered inside:"
echo "$LOG_DIR"
