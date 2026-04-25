#!/bin/bash
# run_combined_analysis.sh
#
# Combined benchmarking script for JBD2 journaling mode comparison.
# Runs two fio workloads CONCURRENTLY in each journaling mode:
#   1. wal_workload.fio  -- sync-heavy, 8KB writes with fsync() after every write
#   2. seq_write.fio     -- bulk 1MB buffered writes, no per-write sync
#
# Each fio file has its own numjobs and group_reporting=1 in [global], so
# each produces exactly one aggregated result block in its JSON output.
# Both fio processes are launched simultaneously and run for the same fixed
# duration (runtime= inside each .fio file), so neither finishes before the
# other and they contend for journal resources for the full window.
#
# A single blktrace and bpftrace instance covers the entire concurrent run.
#
# Prerequisites:
#   - /dev/nvme0n1p6 exists and is already formatted as ext4
#   - wal_workload.fio and seq_write.fio exist in the same directory
#
# Usage: sudo bash run_combined_analysis.sh

# ── Safety check: must run as root ───────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    echo "       Run it with: sudo bash run_combined_analysis.sh"
    exit 1
fi

# ── Configuration variables ───────────────────────────────────────────────────
DEVICE="/dev/nvme0n1p6"
MOUNT_POINT="/media/milan-roy/test_ext4"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
WAL_WORKLOAD_FILE="$PROJECT_DIR/wal_workload.fio"
SEQ_WORKLOAD_FILE="$PROJECT_DIR/seq_write.fio"
JOURNAL_SIZE_MB=64

RESULTS_DIR="$PROJECT_DIR/results/$(date +%Y%m%d_%H%M%S)"

# ── Feature flags ─────────────────────────────────────────────────────────────
ENABLE_JBD2_TRACE=1
ENABLE_FAST_COMMIT=0
# ── Prerequisite checks ───────────────────────────────────────────────────────
echo "============================================================"
echo "  Combined Workload Journaling Mode Analysis"
echo "  JBD2 Benchmarking -- CS614 IIT Kanpur"
echo "============================================================"
echo ""
echo "[1/5] Checking prerequisites..."

if [ ! -b "$DEVICE" ]; then
    echo "ERROR: Block device $DEVICE not found."
    echo "       Verify the partition exists with: lsblk"
    exit 1
fi

if [ ! -f "$WAL_WORKLOAD_FILE" ]; then
    echo "ERROR: WAL workload file not found at $WAL_WORKLOAD_FILE"
    exit 1
fi

if [ ! -f "$SEQ_WORKLOAD_FILE" ]; then
    echo "ERROR: Sequential workload file not found at $SEQ_WORKLOAD_FILE"
    exit 1
fi

if ! command -v fio &> /dev/null; then
    echo "fio not found. Installing..."
    apt-get install -y fio
    echo "fio installed successfully."
else
    echo "  fio:           OK ($(fio --version))"
fi

if ! command -v blktrace &> /dev/null; then
    echo "blktrace not found. Installing..."
    apt-get install -y blktrace
    echo "blktrace installed successfully."
else
    echo "  blktrace:      OK ($(blktrace --version 2>&1 | head -1))"
fi

if ! command -v bpftrace &> /dev/null; then
    echo "bpftrace not found. Installing..."
    apt-get install -y bpftrace
    echo "bpftrace installed successfully."
else
    echo "  bpftrace:      OK ($(bpftrace --version 2>&1))"
fi

JBD2_TRACE_SCRIPT=""
if [ "$ENABLE_JBD2_TRACE" = "1" ]; then
    if [ ! -f "$PROJECT_DIR/trace_jbd2.bt" ]; then
        echo "WARNING: trace_jbd2.bt not found -- JBD2 tracing disabled."
    else
        JBD2_TRACE_SCRIPT="$PROJECT_DIR/trace_jbd2.bt"
    fi
else
    echo "  JBD2 tracing: disabled (ENABLE_JBD2_TRACE=0)"
fi

echo "  device:              OK ($DEVICE)"
echo "  WAL workload file:   OK ($WAL_WORKLOAD_FILE)"
echo "  Seq workload file:   OK ($SEQ_WORKLOAD_FILE)"

# ── Step 1: Verify the NVMe partition ────────────────────────────────────────
echo ""
echo "[2/5] Verifying NVMe partition..."
echo "  Device: $DEVICE"
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "$DEVICE" 2>/dev/null || true

# ── Step 2: Mount the filesystem ─────────────────────────────────────────────
echo ""
echo "[3/5] Mounting filesystem..."

mkdir -p "$MOUNT_POINT"

if mount | grep -q "$DEVICE"; then
    umount -l "$DEVICE"
    echo "  Unmounted existing mount of $DEVICE"
fi
if [ "$ENABLE_FAST_COMMIT" = "1" ]; then
    tune2fs -O fast_commit "$DEVICE" &>/dev/null
    mount -o data=ordered,commit=1 "$DEVICE" "$MOUNT_POINT"
    echo "  Mounted $DEVICE at $MOUNT_POINT (fast_commit enabled)"
else
    tune2fs -O ^fast_commit "$DEVICE" &>/dev/null
    mount -o data=ordered "$DEVICE" "$MOUNT_POINT"
    echo "  Mounted $DEVICE at $MOUNT_POINT"
fi

JOURNAL_SIZE=$(dumpe2fs "$DEVICE" 2>/dev/null | grep "Total journal size" | awk '{print $NF}')
echo "  Journal size:  ${JOURNAL_SIZE:-unknown}"

# ── Step 3: Prepare results directory ────────────────────────────────────────
echo ""
echo "[4/5] Setting up results directory..."
mkdir -p "$RESULTS_DIR"
echo "  Results will be saved to: $RESULTS_DIR"

if ! mountpoint -q /sys/kernel/debug; then
    mount -t debugfs none /sys/kernel/debug
    echo "  debugfs mounted at /sys/kernel/debug"
else
    echo "  debugfs:       OK (already mounted)"
fi

# ── Step 4: Run the benchmark loop ───────────────────────────────────────────
# Both fio processes are launched simultaneously with & and their PIDs
# captured. Each produces its own JSON output file. blktrace and bpftrace
# run for the entire concurrent window and are stopped after both fio
# processes finish.
#
# Output files per mode:
#   wal_<mode>.json             -- fio JSON for WAL workload (one aggregated block)
#   seq_<mode>.json             -- fio JSON for seq workload (one aggregated block)
#   blktrace_<mode>.*           -- blktrace covering the full concurrent run
#   phys_write_bytes_<mode>.txt -- blkparse physical byte count (combined)
#   jbd2_trace_<mode>.txt       -- bpftrace JBD2 trace (combined)
echo ""
echo "[5/5] Running combined workload across all four modes..."
echo "      ordered / journal / writeback / nojournal"
echo "      WAL and seq fio processes run concurrently in each mode."
echo "      Time per mode = fio runtime + setup overhead."
echo ""

for MODE in ordered journal writeback nojournal; do

    echo "------------------------------------------------------------"
    echo "  Running mode: $MODE"
    echo "------------------------------------------------------------"

    # ── Between-mode filesystem reset ─────────────────────────────────────────
    echo "  Cleaning partition for fresh state..."
    find "$MOUNT_POINT" -mindepth 1 -delete
    umount -l "$MOUNT_POINT"

    if [ "$MODE" = "nojournal" ]; then
        tune2fs -O ^has_journal,^fast_commit "$DEVICE" &>/dev/null
        mount "$DEVICE" "$MOUNT_POINT"
    elif [ "$MODE" = "ordered" ] && [ "$ENABLE_FAST_COMMIT" = "1" ]; then
        tune2fs -O has_journal,fast_commit -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
        mount -o data=ordered,commit=1 "$DEVICE" "$MOUNT_POINT"
    else
        tune2fs -O has_journal,^fast_commit -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
        mount -o data=$MODE "$DEVICE" "$MOUNT_POINT"
    fi
    echo "  Partition formatted and mounted in $MODE mode$([ "$MODE" = "ordered" ] && [ "$ENABLE_FAST_COMMIT" = "1" ] && echo " (fast_commit)")."

    if [ "$MODE" = "nojournal" ]; then
        echo "  Active mode confirmed: no-journal (JBD2 disabled, WAF baseline only)"
    else
        ACTIVE_MODE=$(findmnt -no OPTIONS "$MOUNT_POINT" | tr ',' '\n' | grep '^data=')
        [ -z "$ACTIVE_MODE" ] && ACTIVE_MODE="data=ordered (kernel default, not shown in mounts)"
        FC_STATUS=$(tune2fs -l "$DEVICE" 2>/dev/null | grep "Filesystem features" | grep -o "fast_commit" || true)
        [ -n "$FC_STATUS" ] && ACTIVE_MODE="$ACTIVE_MODE fast_commit=on" || ACTIVE_MODE="$ACTIVE_MODE fast_commit=off"
        echo "  Active mode confirmed: $ACTIVE_MODE"
    fi

    echo "  Dropping page cache..."
    echo 3 > /proc/sys/vm/drop_caches

    # ── Start blktrace ────────────────────────────────────────────────────────
    blktrace -d /dev/nvme0n1p6 -D "$RESULTS_DIR" -o "blktrace_${MODE}" &
    BLKTRACE_PID=$!
    sleep 0.5

    # ── Start bpftrace JBD2 tracer ────────────────────────────────────────────
    BPFTRACE_PID=""
    if [ -n "$JBD2_TRACE_SCRIPT" ]; then
        bpftrace "$JBD2_TRACE_SCRIPT" \
            > "$RESULTS_DIR/jbd2_trace_${MODE}.txt" 2>&1 &
        BPFTRACE_PID=$!
        sleep 0.5
        echo "  bpftrace JBD2 tracer started (PID $BPFTRACE_PID)"
    fi

    # ── Launch both fio processes concurrently ────────────────────────────────
    # Each has group_reporting=1 in its own [global], so each produces exactly
    # one aggregated result block regardless of numjobs.
    echo "  Launching both fio processes concurrently..."
    echo "    WAL: $WAL_WORKLOAD_FILE"
    echo "    Seq: $SEQ_WORKLOAD_FILE"
    echo ""

    fio "$WAL_WORKLOAD_FILE" \
        --directory="$MOUNT_POINT" \
        --output-format=json \
        --output="$RESULTS_DIR/wal_${MODE}.json" &
    FIO_WAL_PID=$!

    fio "$SEQ_WORKLOAD_FILE" \
        --directory="$MOUNT_POINT" \
        --output-format=json \
        --output="$RESULTS_DIR/seq_${MODE}.json" &
    FIO_SEQ_PID=$!

    # Wait for both; capture exit codes individually.
    wait "$FIO_WAL_PID"
    FIO_WAL_EXIT=$?
    wait "$FIO_SEQ_PID"
    FIO_SEQ_EXIT=$?

    if [ $FIO_WAL_EXIT -ne 0 ] || [ $FIO_SEQ_EXIT -ne 0 ]; then
        echo "ERROR: One or both fio processes failed for mode '$MODE'."
        [ $FIO_WAL_EXIT -ne 0 ] && echo "       WAL exit code: $FIO_WAL_EXIT"
        [ $FIO_SEQ_EXIT -ne 0 ] && echo "       Seq exit code: $FIO_SEQ_EXIT"
        echo "       Likely out of disk space. Check: df -h $MOUNT_POINT"
        kill "$BLKTRACE_PID" 2>/dev/null; wait "$BLKTRACE_PID" 2>/dev/null
        [ -n "$BPFTRACE_PID" ] && kill "$BPFTRACE_PID" 2>/dev/null
        [ -n "$BPFTRACE_PID" ] && wait "$BPFTRACE_PID" 2>/dev/null
        umount "$MOUNT_POINT"
        exit 1
    fi

    echo "  Both fio processes finished."
    sync -f "$MOUNT_POINT"

    # ── Stop bpftrace ─────────────────────────────────────────────────────────
    if [ -n "$BPFTRACE_PID" ]; then
        kill "$BPFTRACE_PID" 2>/dev/null
        wait "$BPFTRACE_PID" 2>/dev/null
        echo "  bpftrace: JBD2 trace saved to jbd2_trace_${MODE}.txt"
    fi

    # ── Stop blktrace and compute physical write bytes ────────────────────────
    kill "$BLKTRACE_PID" 2>/dev/null
    wait "$BLKTRACE_PID" 2>/dev/null

    blkparse -i "$RESULTS_DIR/blktrace_${MODE}" -f "%a %d %N\n" -q 2>/dev/null \
        | awk '$1=="C" && $2~/W/ {sum+=$3} END {print sum+0}' \
        > "$RESULTS_DIR/phys_write_bytes_${MODE}.txt"
    echo "  blktrace: physical write bytes saved to phys_write_bytes_${MODE}.txt"

    echo ""
    echo "  Mode $MODE complete. Results saved to $RESULTS_DIR"
    echo ""

done

# ── Restore filesystem after nojournal baseline run ───────────────────────────
echo "Restoring journal..."
umount -l "$MOUNT_POINT"
if [ "$ENABLE_FAST_COMMIT" = "1" ]; then
    tune2fs -O has_journal,fast_commit -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
    mount -o data=ordered,commit=1 "$DEVICE" "$MOUNT_POINT"
    echo "  Journal restored (fast_commit enabled)."
else
    tune2fs -O has_journal,^fast_commit -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
    mount -o data=ordered "$DEVICE" "$MOUNT_POINT"
    echo "  Journal restored."
fi

# ── Step 5: Analyse and print the comparison ─────────────────────────────────
echo "============================================================"
echo "  ANALYSIS: Calling analyse_combined_results.py..."
echo "============================================================"

ANALYSIS_SCRIPT="$PROJECT_DIR/analyse_combined_results.py"
if [ ! -f "$ANALYSIS_SCRIPT" ]; then
    echo "ERROR: Analysis script not found at $ANALYSIS_SCRIPT"
    echo "       Raw JSON results are saved in: $RESULTS_DIR"
    umount "$MOUNT_POINT"
    exit 1
fi

python3 "$ANALYSIS_SCRIPT" "$RESULTS_DIR"

echo ""
echo "Unmounting filesystem..."
umount "$MOUNT_POINT"
echo ""
echo "Done. To re-run the analysis on saved results without re-benchmarking:"
echo "  python3 analyse_combined_results.py $RESULTS_DIR"
