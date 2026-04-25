#!/bin/bash
set -e

# Prompt User for Hardware Topology
read -p "Enter Target Device [default: /dev/nvme0n1p7]: " DEV
DEV=${DEV:-/dev/nvme0n1p7}

read -p "Enter Mount Path [default: /mnt/analysis_2]: " MNT
MNT=${MNT:-/mnt/analysis_2}

# Automatically use the current directrory
MODULE_DIR=$(pwd)
RES_DIR="$MODULE_DIR/benchmark_results"

sudo mkdir -p $RES_DIR
echo "Installing dependencies..."
sudo apt-get update -y && sudo apt-get install -y fio sysbench python3-matplotlib python3-numpy fsmark dbench 2>/dev/null || true

run_benchmark() {
    local DATA_MODE=$1
    local OPT=$2
    local THREADS=$3

    echo "=================================================================="
    echo "Running Mode=$DATA_MODE, Opt=$OPT, Threads=$THREADS"
    echo "=================================================================="

    # Remount Partition safely
    sudo umount $MNT || true
    sudo mount -o data=$DATA_MODE $DEV $MNT

    # Swap Module Configuration
    sudo rmmod jbd2_trace 2>/dev/null || true
    # Feed specifically formatted device name directly into C module scope dynamically
    DEV_NAME=${DEV#/dev/}
    sudo insmod $MODULE_DIR/jbd2_trace.ko optimize=$OPT device=$DEV_NAME

    for run_id in 1 2 3; do
        local PREFIX="${DATA_MODE}_opt${OPT}_t${THREADS}_r${run_id}"
        echo ">>> Executing Run $run_id/3 for Threads=$THREADS <<<"
        
        # 1. FIO
        echo "-> FIO"
        sudo fio --name=fio --directory=$MNT --ioengine=sync --rw=randwrite --bs=4k --numjobs=$THREADS --size=4M --fsync=1 --group_reporting --output-format=json > $RES_DIR/${PREFIX}_fio.json

        # 2. Sysbench
        echo "-> sysbench"
        cd $MNT
        sudo sysbench fileio --file-total-size=4M --file-test-mode=rndwr prepare > /dev/null
        sudo sysbench fileio --file-total-size=4M --file-test-mode=rndwr --threads=$THREADS --file-fsync-all=on --time=10 run > $RES_DIR/${PREFIX}_sysbench.txt
        sudo sysbench fileio --file-total-size=4M cleanup > /dev/null
        cd $MODULE_DIR

        # 3. fs_mark
        echo "-> fs_mark"
        sudo mkdir -p $MNT/fsmark
        sudo fs_mark -d $MNT/fsmark -n 1000 -s 4096 -t $THREADS -S 1 > $RES_DIR/${PREFIX}_fsmark.txt
        sudo rm -rf $MNT/fsmark

        # 4. dbench
        echo "-> dbench"
        sudo dbench -D $MNT -t 10 $THREADS > $RES_DIR/${PREFIX}_dbench.txt

    done

    # Dump kernel module summary to dmesg queue
    sudo rmmod jbd2_trace
}

# The Automation Loop 
for mode in "ordered" "writeback"; do
    for opt in 0 1; do
        # Removed 32 threads. Scaling natively 1, 4, 8, 16 
        for t in 1 4 8 16; do
            run_benchmark $mode $opt $t
        done
    done
done

echo "=========================================================="
echo "All tests complete! Run 'python3 plot_results.py' next!"
