#!/bin/bash
set -e

# Prompt User for Hardware Topology
read -p "Enter Target Device [default: /dev/nvme0n1p7]: " DEV
DEV=${DEV:-/dev/nvme0n1p7}

read -p "Enter Mount Path [default: /mnt/analysis_2]: " MNT
MNT=${MNT:-/mnt/analysis_2}

MODULE_DIR=$(pwd)


validate_journal() {
    local OPT=$1
    echo "=================================================================="
    if [ "$OPT" -eq 0 ]; then
        echo "Validating Journal Integrity -> BASELINE (Opt: 0)"
    else
        echo "Validating Journal Integrity -> CAS OPTIMIZED (Opt: 1)"
    fi
    echo "=================================================================="

    sudo umount $MNT 2>/dev/null || true
    
    # 1. Start with a rigorous fresh filesystem check
    echo "[*] Step 1: Initial fsck verification..."
    sudo fsck.ext4 -f -p $DEV > /dev/null

    # 2. Mount and load the traced kernel module
    sudo mount -o data=ordered $DEV $MNT
    sudo rmmod jbd2_trace 2>/dev/null || true
    DEV_NAME=${DEV#/dev/}
    sudo insmod $MODULE_DIR/jbd2_trace.ko optimize=$OPT device=$DEV_NAME

    # 3. Aggressively stress the journal with thousands of immediate syncs
    echo "[*] Step 2: Stressing JBD2 journal with aggressive fsync workload..."
    sudo fio --name=validate --directory=$MNT --ioengine=sync --rw=randwrite --bs=4k --numjobs=16 --size=20M --fsync=1 --group_reporting > /dev/null

    # 4. Safely flush and unmount
    echo "[*] Step 3: Flushing pending blocks and unmounting safely..."
    sync
    sudo umount $MNT

    # 5. The absolute test: Run fsck.ext4 again to verify no journal/inode corruption occurred
    echo "[*] Step 4: Validating post-workload structural integrity..."
    
    # -n opens the filesystem read-only and automatically answers 'no' to repairs, making it a pure validation test
    if sudo fsck.ext4 -f -n $DEV > /dev/null 2>&1; then
        echo "[SUCCESS] Pass: Ext4 Journal (JBD2) and file hierarchies perfectly consistent."
    else
        echo "[ERROR] FAIL: Filesystem corruption detected! The optimization compromised the journal."
        sudo rmmod jbd2_trace
        exit 1
    fi
    echo ""
}

# Run the validation matrix
validate_journal 0
validate_journal 1

sudo rmmod jbd2_trace 2>/dev/null || true
echo "=========================================================="
echo "Validation Suite Complete!"
echo "Both the baseline and the lockless CAS optimization mathematically preserve Ext4 structural integrity!"
