#!/bin/bash
# fjm_full_benchmark.sh
#
# Full FJM Benchmark: WAL Workload across all compatible journaling modes.
#
# Runs 6 benchmarks total:
#   ordered   (ext4_tracker, no FJM)  vs  ordered+fjm  (ext4_tracker, FJM on)
#   journal   (ext4_tracker, no FJM)  vs  journal+fjm  (ext4_tracker, FJM on)
#   writeback (ext4_tracker, no FJM)  vs  writeback+fjm(ext4_tracker, FJM on)
#
# NOTE: nojournal mode is intentionally excluded — FJM hooks into JBD2's
# block allocator. Without a journal, JBD2 is not active and FJM has nothing
# to attach to.
#
# Device:  /dev/sdb  (5.1G)
# Journal: 64MB (safe for 5.1G device)
# Workload: 100MB per run (from wal_workload.fio)
#
# Usage: sudo bash fjm_full_benchmark.sh

# ── Safety check ─────────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Run as root. Use: sudo bash fjm_full_benchmark.sh"
    exit 1
fi

# ── Configuration ─────────────────────────────────────────────────────────────
DEVICE="/dev/sdb"
MOUNT_POINT="/mnt/test_ext4"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_FILE="$SCRIPT_DIR/wal_workload.fio"
JOURNAL_SIZE_MB=64
RESULTS_DIR="$SCRIPT_DIR/fjm_full_results/$(date +%Y%m%d_%H%M%S)"

# ── Banner ────────────────────────────────────────────────────────────────────
echo "============================================================"
echo "  FJM Full Mode Benchmark"
echo "  6 runs: ordered / journal / writeback  ×  std / fjm"
echo "  Device: $DEVICE (5.1G)    Journal: ${JOURNAL_SIZE_MB}MB"
echo "============================================================"
echo ""

# ── Prerequisite checks ───────────────────────────────────────────────────────
echo "[1/5] Checking prerequisites..."

if [ ! -b "$DEVICE" ]; then
    echo "ERROR: Block device $DEVICE not found."
    exit 1
fi

if [ ! -f "$WORKLOAD_FILE" ]; then
    echo "ERROR: Workload file not found: $WORKLOAD_FILE"
    echo "       Copy wal_workload.fio next to this script."
    exit 1
fi

if ! command -v fio &>/dev/null; then
    echo "  fio not found. Installing..."
    apt-get install -y fio || { echo "ERROR: fio install failed. Run: sudo dpkg --configure -a && sudo apt-get install fio"; exit 1; }
fi
echo "  fio:       OK ($(fio --version))"

if ! command -v blktrace &>/dev/null; then
    echo "  blktrace not found. Installing..."
    apt-get install -y blktrace
fi
echo "  blktrace:  OK"

if ! command -v bc &>/dev/null; then
    apt-get install -y bc &>/dev/null
fi

# Verify ext4_tracker module is loaded
if ! cat /proc/filesystems 2>/dev/null | grep -q "ext4_tracker"; then
    echo ""
    echo "WARNING: ext4_tracker is NOT registered in /proc/filesystems."
    echo "         The FJM runs will fail to mount."
    echo "         Load the module first: sudo insmod <path>/ext4_tracker.ko"
    echo "         Continuing anyway (std runs will still work)..."
fi
echo "  device:    OK ($DEVICE)"
echo "  workload:  OK ($WORKLOAD_FILE)"

# ── Setup ─────────────────────────────────────────────────────────────────────
echo ""
echo "[2/5] Verifying device..."
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "$DEVICE" 2>/dev/null || true

echo ""
echo "[3/5] Setting up results directory..."
mkdir -p "$RESULTS_DIR"
mkdir -p "$MOUNT_POINT"
echo "  Results: $RESULTS_DIR"

if ! mountpoint -q /sys/kernel/debug; then
    mount -t debugfs none /sys/kernel/debug
fi
echo "  debugfs:   OK"

# ── Helper: run one benchmark ─────────────────────────────────────────────────
# Args: $1 = label (e.g. "ordered_std")
#       $2 = data mode (ordered | journal | writeback)
#       $3 = fjm flag ("fjm" or "")
run_one() {
    local LABEL="$1"
    local DATA_MODE="$2"
    local FJM_FLAG="$3"

    # Build mount options
    local MOUNT_OPTS="data=${DATA_MODE}"
    [ -n "$FJM_FLAG" ] && MOUNT_OPTS="${MOUNT_OPTS},${FJM_FLAG}"

    echo ""
    echo "------------------------------------------------------------"
    echo "  Run: $LABEL  (mount -t ext4_tracker -o $MOUNT_OPTS)"
    echo "------------------------------------------------------------"

    # Clean unmount
    umount "$DEVICE"     2>/dev/null
    umount "$MOUNT_POINT" 2>/dev/null
    sleep 1

    # Fresh format for every run (ensures identical starting conditions)
    echo "  Formatting $DEVICE (journal=${JOURNAL_SIZE_MB}MB)..."
    mkfs.ext4 -F -b 4096 \
        -J size=$JOURNAL_SIZE_MB \
        -E lazy_itable_init=0,lazy_journal_init=0 \
        "$DEVICE" >/dev/null 2>&1
    echo "  Format done."

    # Mount
    mount -t ext4_tracker -o "$MOUNT_OPTS" "$DEVICE" "$MOUNT_POINT"
    if [ $? -ne 0 ]; then
        echo "ERROR: Mount failed for $LABEL. Skipping."
        echo "       If FJM run: is ext4_tracker.ko loaded?"
        return 1
    fi
    echo "  Mounted OK."

    # Sanity: free space check
    FREE_MB=$(df -m "$MOUNT_POINT" | awk 'NR==2 {print $4}')
    echo "  Free space: ${FREE_MB}MB"
    if [ "${FREE_MB:-0}" -lt 200 ]; then
        echo "WARNING: Low disk space. fio may fail."
    fi

    # Drop page cache
    echo 3 > /proc/sys/vm/drop_caches

    # Clear dmesg so FJM messages are easy to isolate
    dmesg -c > /dev/null

    # Start blktrace
    blktrace -d "$DEVICE" -D "$RESULTS_DIR" -o "blktrace_${LABEL}" &
    BLKTRACE_PID=$!
    sleep 0.5

    # Run fio
    echo "  Running fio (100MB)..."
    fio "$WORKLOAD_FILE" \
        --directory="$MOUNT_POINT" \
        --output-format=json \
        --output="$RESULTS_DIR/wal_${LABEL}.json"
    FIO_EXIT=$?

    sync -f "$MOUNT_POINT"

    # Stop blktrace
    kill "$BLKTRACE_PID" 2>/dev/null
    wait "$BLKTRACE_PID" 2>/dev/null

    if [ $FIO_EXIT -ne 0 ]; then
        echo "ERROR: fio failed (exit $FIO_EXIT) for $LABEL. Skipping results."
        umount "$MOUNT_POINT" 2>/dev/null
        return 1
    fi

    # Compute physical bytes written (WAF numerator)
    local BLKTRACE_OUT="$RESULTS_DIR/blktrace_${LABEL}"
    blkparse -i "$BLKTRACE_OUT" -f "%a %d %N\n" -q 2>/dev/null \
        | awk '$1=="C" && $2~/W/ {sum+=$3} END {print sum+0}' \
        > "$RESULTS_DIR/phys_write_bytes_${LABEL}.txt"

    # Save FJM dmesg for this run
    dmesg | grep "FJM:" > "$RESULTS_DIR/fjm_dmesg_${LABEL}.txt" 2>/dev/null
    local FREED=$(grep -c "FJM: Freed" "$RESULTS_DIR/fjm_dmesg_${LABEL}.txt" 2>/dev/null || echo 0)
    local COMMITTED=$(grep -c "FJM: committed" "$RESULTS_DIR/fjm_dmesg_${LABEL}.txt" 2>/dev/null || echo 0)
    echo "  FJM events — Freed: $FREED  Committed: $COMMITTED"

    umount "$MOUNT_POINT"
    echo "  ✓ $LABEL complete."
}

# ── Step 4: Run all 6 benchmarks ─────────────────────────────────────────────
echo ""
echo "[4/5] Running 6 benchmarks..."
echo "      Each reformats the device and writes 100MB."
echo ""

# Standard ext4_tracker (FJM disabled) — 3 modes
run_one "ordered_std"   "ordered"   ""
run_one "journal_std"   "journal"   ""
run_one "writeback_std" "writeback" ""

# FJM-enabled ext4_tracker — 3 modes
run_one "ordered_fjm"   "ordered"   "fjm"
run_one "journal_fjm"   "journal"   "fjm"
run_one "writeback_fjm" "writeback" "fjm"

# ── Step 5: Print comparison table ───────────────────────────────────────────
echo ""
echo "============================================================"
echo "[5/5] Results Comparison"
echo "============================================================"
echo ""

# Helper to extract a stat from a fio JSON file
extract_fio() {
    local JSON="$1"
    local FIELD="$2"
    python3 -c "
import json
try:
    d = json.load(open('$JSON'))
    w = d['jobs'][0]['write']
    if '$FIELD' == 'bw':
        print(round(w['bw']))
    elif '$FIELD' == 'iops':
        print(round(w['iops'], 1))
    elif '$FIELD' == 'lat_p99':
        b = w.get('lat_ns', {}).get('percentile', {})
        k = [x for x in b if x.startswith('99.0')]
        print(round(int(b[k[0]])/1000/1000, 3) if k else 'N/A')
except:
    print('N/A')
" 2>/dev/null
}

# Print header
printf "  %-18s %10s %10s %10s %12s %10s %10s\n" \
    "RUN" "BW(KB/s)" "IOPS" "P99(ms)" "Logical(MB)" "Phys(MB)" "WAF"
printf "  %-18s %10s %10s %10s %12s %10s %10s\n" \
    "------------------" "--------" "----" "-------" "-----------" "--------" "---"

for LABEL in ordered_std ordered_fjm journal_std journal_fjm writeback_std writeback_fjm; do
    JSON="$RESULTS_DIR/wal_${LABEL}.json"
    PHYS_FILE="$RESULTS_DIR/phys_write_bytes_${LABEL}.txt"

    if [ ! -f "$JSON" ]; then
        printf "  %-18s %10s\n" "$LABEL" "(no results)"
        continue
    fi

    BW=$(extract_fio "$JSON" "bw")
    IOPS=$(extract_fio "$JSON" "iops")
    LAT=$(extract_fio "$JSON" "lat_p99")
    PHYS_BYTES=$(cat "$PHYS_FILE" 2>/dev/null || echo 0)
    PHYS_MB=$(echo "scale=1; $PHYS_BYTES / 1048576" | bc 2>/dev/null || echo "?")
    WAF=$(echo "scale=2; $PHYS_BYTES / (100 * 1048576)" | bc 2>/dev/null || echo "?")

    printf "  %-18s %10s %10s %10s %12s %10s %10s\n" \
        "$LABEL" "$BW" "$IOPS" "$LAT" "100" "$PHYS_MB" "${WAF}x"
done

echo ""
echo "------------------------------------------------------------"
echo "  FJM Checkpoint Events (only meaningful for FJM runs)"
echo "------------------------------------------------------------"
for LABEL in ordered_fjm journal_fjm writeback_fjm; do
    DMESG="$RESULTS_DIR/fjm_dmesg_${LABEL}.txt"
    if [ -f "$DMESG" ]; then
        FREED=$(grep -c "FJM: Freed" "$DMESG" 2>/dev/null || echo 0)
        COMMITTED=$(grep -c "FJM: committed" "$DMESG" 2>/dev/null || echo 0)
        IDX_BLOCKS=$(grep "FJM: initialised" "$DMESG" | grep -oP 'index_blocks=\K[0-9]+' | tail -1)
        printf "  %-18s  Freed=%-6s Committed=%-6s IndexBlocks=%s\n" \
            "$LABEL" "$FREED" "$COMMITTED" "${IDX_BLOCKS:-N/A}"
    fi
done

echo ""
echo "Raw results: $RESULTS_DIR"
echo "Done."
