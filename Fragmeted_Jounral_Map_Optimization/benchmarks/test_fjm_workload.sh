#!/bin/bash
DEVICE=/dev/sdb
MNT=/mnt/testfs

# Ensure it's cleanly unmounted first
umount $DEVICE 2>/dev/null
umount $MNT 2>/dev/null

# Format the device strictly
mkfs.ext4 -F \
    -E lazy_itable_init=0,lazy_journal_init=0 \
    $DEVICE

mkdir -p $MNT

# Clear dmesg log buffer before mount so we capture mount initialization FJM logs
sudo dmesg -c > /dev/null

# Mount using the custom ext4_tracker type and our new fragmented journal mount option!
sudo mount -t ext4_tracker -o fjm $DEVICE $MNT
echo "ext4 mounted on $MNT with FJM enabled"

echo "=== Running Heavy & Diverse FJM Workload on $DEVICE ==="

# 1. METADATA STRESS (Parallel small file and dir creation)
# This floods JBD2 with thousands of tiny metadata updates, aggressively testing
# FJM's spinlock and rapidly fragmenting the transaction layout.
echo " -> [1/4] Spawning aggressive parallel metadata..."
for i in {1..20}; do
    (
        mkdir -p $MNT/dir_$i
        for j in {1..50}; do
            touch $MNT/dir_$i/tiny_meta_$j
        done
    ) &
done

# 2. LARGE PAYLOAD STRESS (Multi-block transaction fragments)
# Writes chunked large files simultaneously to force JBD2 to request 
# massive arrays of non-contiguous fragments at once.
echo " -> [2/4] Writing large parallel data blobs..."
for i in {1..10}; do
    (
        dd if=/dev/urandom of=$MNT/large_blob_$i.bin bs=1M count=3 status=none
    ) &
done

# Wait for 1 and 2 to finish creating files
wait

echo " -> Forcing commit and waiting 6 seconds for transaction rotation..."
sync
sleep 6

# 3. FRAGMENTATION INDUCTION (Freeing blocks asynchronously)
# By deleting subsets of data, JBD2 will checkpoint and FJM will aggressively
# clear random bits in its bitmap, creating "holes" throughout the journal.
echo " -> [3/4] Truncating and deleting to punch fragmented holes..."
for i in {1..10}; do
    rm -rf $MNT/dir_$i   # Deleting half the directories
done
rm -f $MNT/large_blob_1.bin
rm -f $MNT/large_blob_3.bin
sync   # Force JBD2 commit
echo " -> Sleeping another 6 seconds so kjournald2 checkpoints old data to disk and FJM clears the bitmap bits..."
sleep 6

# 4. HOLE FILLING (Targeted operations to land inside the fragments)
# Now we run small discrete I/Os. FJM's first-fit allocator will hunt for the 
# free holes created in step 3, highly scrambling the block numbering in the logs!
echo " -> [4/4] Filling fragmented journal holes..."
for i in {1..50}; do
    echo "This is testing the allocator gap filling" > $MNT/dir_15/frag_fill_$i.txt
    ln -s $MNT/dir_15/frag_fill_$i.txt $MNT/sym_$i
done

# Force everything to be safely checkpointed
sync

echo "=== Unmounting ==="
#sudo umount $MNT

echo "=== FJM Kernel Logs ==="
# Dump detailed logs from throughout the lifecycle
echo "=========================================================="
echo "Mount Initialization phase:"
sudo dmesg | grep "FJM: initialised" 

echo "=========================================================="
echo "Sample of Transaction Starts:"
sudo dmesg | grep "FJM: Tracking new transaction" | head -n 10

echo "=========================================================="
echo "Sample of Fragmented Block Allocations (Notice the numbering):"
# Specifically tailing this part so you can see if the blocks jump out of sequential order
sudo dmesg | grep "FJM: Allocated journal blk=" | tail -n 20

