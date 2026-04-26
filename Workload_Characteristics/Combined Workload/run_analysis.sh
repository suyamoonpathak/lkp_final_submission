#!/bin/bash
# run_analysis.sh
#
# Combined workload benchmarking: runs sync-heavy and seq-write fio jobs
# simultaneously across all four journaling modes, runs N times, and
# produces an averaged comparison table.
#
# Usage: sudo bash run_analysis.sh

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    echo "       Run it with: sudo bash run_analysis.sh"
    exit 1
fi

echo "============================================================"
echo "  Combined Workload Journaling Mode Analysis"
echo "  JBD2 Benchmarking -- CS614 IIT Kanpur"
echo "============================================================"
echo ""

# ── Interactive configuration ─────────────────────────────────────────────────
echo "  Please enter benchmark configuration (press Enter to accept defaults):"
echo ""

read -rp "  Device (default: /dev/nvme0n1p6): " DEVICE
DEVICE="${DEVICE:-/dev/nvme0n1p6}"

read -rp "  Mount point (default: /media/milan-roy/test_ext4): " MOUNT_POINT
MOUNT_POINT="${MOUNT_POINT:-/media/milan-roy/test_ext4}"

read -rp "  Runtime per mode in seconds, enter number only (default: 120): " RUNTIME
RUNTIME="${RUNTIME:-120}"

read -rp "  Block size for sync-heavy workload, suffix k/m/g required (default: 8k): " BS_SYNC
BS_SYNC="${BS_SYNC:-8k}"

read -rp "  Block size for seq-write workload, suffix k/m/g required (default: 1m): " BS_SEQ
BS_SEQ="${BS_SEQ:-1m}"

read -rp "  Number of repeated runs N (default: 5): " N_INPUT
N_INPUT="${N_INPUT:-5}"
if ! [[ "$N_INPUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: N must be a positive integer."
    exit 1
fi
N=$N_INPUT

echo ""
echo "  Configuration summary:"
echo "    Device          : $DEVICE"
echo "    Mount point     : $MOUNT_POINT"
echo "    Runtime         : ${RUNTIME} seconds"
echo "    BS (sync-heavy) : $BS_SYNC"
echo "    BS (seq-write)  : $BS_SEQ"
echo "    Runs (N)        : $N"
echo ""
read -rp "  Proceed? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_FIO="$PROJECT_DIR/sync_heavy_workload.fio"
SEQ_FIO="$PROJECT_DIR/seq_write_workload.fio"
JOURNAL_SIZE_MB=64

# ── Prerequisite checks ───────────────────────────────────────────────────────
echo ""
echo "[1/5] Checking prerequisites..."

if [ ! -b "$DEVICE" ]; then
    echo "ERROR: Block device $DEVICE not found."
    echo "       Verify the partition exists with: lsblk"
    exit 1
fi

for f in "$SYNC_FIO" "$SEQ_FIO"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: Workload file not found: $f"
        exit 1
    fi
done

if ! command -v fio &> /dev/null; then
    echo "fio not found. Installing..."
    apt-get install -y fio
else
    echo "  fio:           OK ($(fio --version))"
fi

if ! command -v blktrace &> /dev/null; then
    echo "blktrace not found. Installing..."
    apt-get install -y blktrace
else
    echo "  blktrace:      OK ($(blktrace --version 2>&1 | head -1))"
fi

echo "  device:        OK ($DEVICE)"
echo "  sync fio:      OK ($SYNC_FIO)"
echo "  seq fio:       OK ($SEQ_FIO)"

# ── Ensure debugfs is mounted ─────────────────────────────────────────────────
if ! mountpoint -q /sys/kernel/debug; then
    mount -t debugfs none /sys/kernel/debug
    echo "  debugfs mounted at /sys/kernel/debug"
else
    echo "  debugfs:       OK (already mounted)"
fi

# ── Verify partition ──────────────────────────────────────────────────────────
echo ""
echo "[2/5] Verifying partition..."
echo "  Device: $DEVICE"
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "$DEVICE" 2>/dev/null || true

# ── Mount filesystem ──────────────────────────────────────────────────────────
echo ""
echo "[3/5] Mounting filesystem..."
mkdir -p "$MOUNT_POINT"
if mount | grep -q "$DEVICE"; then
    umount -l "$DEVICE"
    echo "  Unmounted existing mount of $DEVICE"
fi
tune2fs -O has_journal -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
mount -o data=ordered "$DEVICE" "$MOUNT_POINT"
echo "  Mounted $DEVICE at $MOUNT_POINT"

JOURNAL_SIZE=$(dumpe2fs "$DEVICE" 2>/dev/null | grep "Total journal size" | awk '{print $NF}')
echo "  Journal size:  ${JOURNAL_SIZE:-unknown}"

# ── Repeated runs ─────────────────────────────────────────────────────────────
echo ""
echo "[4/5] Setting up results directories..."

RUN_DIRS=()

for run_i in $(seq 1 "$N"); do
    echo ""
    echo "############################################################"
    echo "  RUN $run_i of $N"
    echo "############################################################"

    RESULTS_DIR="$PROJECT_DIR/results/combined_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$RESULTS_DIR"
    echo "  Results directory: $RESULTS_DIR"
    RUN_DIRS+=("$RESULTS_DIR")

    echo ""
    echo "[5/5] Running combined workload across all four modes..."
    echo "      Modes    : ordered / journal / writeback / nojournal"
    echo "      Runtime  : ${RUNTIME} seconds"
    echo "      BS sync  : $BS_SYNC   BS seq: $BS_SEQ"
    echo ""

    for MODE in ordered journal writeback nojournal; do

        echo "------------------------------------------------------------"
        echo "  Mode: $MODE"
        echo "------------------------------------------------------------"

        echo "  Cleaning partition for fresh state..."
        find "$MOUNT_POINT" -mindepth 1 -delete
        umount -l "$MOUNT_POINT"

        if [ "$MODE" = "nojournal" ]; then
            tune2fs -O ^has_journal "$DEVICE" &>/dev/null
            mount "$DEVICE" "$MOUNT_POINT"
        else
            tune2fs -O has_journal -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
            mount -o data=$MODE "$DEVICE" "$MOUNT_POINT"
        fi

        echo "  Dropping page cache..."
        echo 3 > /proc/sys/vm/drop_caches

        if [ "$MODE" = "nojournal" ]; then
            echo "  Active mode confirmed: no-journal (JBD2 disabled)"
        else
            ACTIVE_MODE=$(findmnt -no OPTIONS "$MOUNT_POINT" | tr ',' '\n' | grep '^data=')
            [ -z "$ACTIVE_MODE" ] && ACTIVE_MODE="data=ordered (kernel default)"
            echo "  Active mode confirmed: $ACTIVE_MODE"
        fi
        echo "  Starting combined benchmark (runtime=${RUNTIME}s)..."
        echo ""

        # ── Run both fio jobs simultaneously ──────────────────────────────────
        # Each writes to its own file to avoid contention on the same inode.
        # They are launched as background processes and we wait for both.
        fio "$SYNC_FIO" \
            --directory="$MOUNT_POINT" \
            --runtime="$RUNTIME" \
            --bs="$BS_SYNC" \
            --output-format=json \
            --output="$RESULTS_DIR/combined_${MODE}_sync.json" &
        FIO_SYNC_PID=$!

        fio "$SEQ_FIO" \
            --directory="$MOUNT_POINT" \
            --runtime="$RUNTIME" \
            --bs="$BS_SEQ" \
            --output-format=json \
            --output="$RESULTS_DIR/combined_${MODE}_seq.json" &
        FIO_SEQ_PID=$!

        SYNC_OK=0
        SEQ_OK=0
        wait "$FIO_SYNC_PID" && SYNC_OK=1
        wait "$FIO_SEQ_PID"  && SEQ_OK=1

        if [ $SYNC_OK -eq 0 ] || [ $SEQ_OK -eq 0 ]; then
            echo "ERROR: fio failed for mode '$MODE' (sync_ok=$SYNC_OK seq_ok=$SEQ_OK)."
            umount "$MOUNT_POINT"
            exit 1
        fi

        sync -f "$MOUNT_POINT"

        echo "  Mode $MODE complete."
        echo ""

    done

    # ── Restore filesystem ────────────────────────────────────────────────────
    echo "Restoring journal..."
    umount -l "$MOUNT_POINT"
    tune2fs -O has_journal -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
    mount -o data=ordered "$DEVICE" "$MOUNT_POINT"
    echo "  Journal restored."

    if [ $run_i -lt $N ]; then
        echo "  Waiting 5 seconds before next run..."
        sleep 5
    fi
done

# ── Analysis ──────────────────────────────────────────────────────────────────
echo "============================================================"
echo "  ANALYSIS: Calling analyse_results.py..."
echo "============================================================"

ANALYSIS_SCRIPT="$PROJECT_DIR/analyse_results.py"
if [ ! -f "$ANALYSIS_SCRIPT" ]; then
    echo "ERROR: Analysis script not found at $ANALYSIS_SCRIPT"
    umount "$MOUNT_POINT"
    exit 1
fi

python3 "$ANALYSIS_SCRIPT" \
    --bs-sync "$BS_SYNC" \
    --bs-seq  "$BS_SEQ" \
    -- "${RUN_DIRS[@]}"

# ── Cleanup ───────────────────────────────────────────────────────────────────
echo ""
echo "Unmounting filesystem..."
umount "$MOUNT_POINT"
echo ""
echo "Done."
echo "  Results saved in:"
for d in "${RUN_DIRS[@]}"; do
    echo "    $d"
done
echo ""
echo "  Re-run analysis only:"
echo "    python3 analyse_results.py --bs-sync $BS_SYNC --bs-seq $BS_SEQ -- ${RUN_DIRS[*]}"
