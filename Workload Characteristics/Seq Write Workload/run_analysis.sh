#!/bin/bash
# run_analysis.sh
#
# Sync-heavy workload benchmarking script for JBD2 journaling mode comparison.
# Benchmarks all four journaling modes across multiple block sizes,
# runs N times, and produces averaged comparison tables.
#
# Usage: sudo bash run_analysis.sh

# ── Safety check: must run as root ───────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    echo "       Run it with: sudo bash run_analysis.sh"
    exit 1
fi

echo "============================================================"
echo "  Sequential Write Workload Journaling Mode Analysis"
echo "  JBD2 Benchmarking -- CS614 IIT Kanpur"
echo "============================================================"
echo ""

# ── Interactive configuration ─────────────────────────────────────────────────
echo "  Please enter benchmark configuration (press Enter to accept defaults):"
echo ""

read -rp "  Config label (e.g. 'nvme_ext4', default: 'default'): " CONFIG
CONFIG="${CONFIG:-default}"

read -rp "  Device (default: /dev/nvme0n1p6): " DEVICE
DEVICE="${DEVICE:-/dev/nvme0n1p6}"

read -rp "  Mount point (default: /media/milan-roy/test_ext4): " MOUNT_POINT
MOUNT_POINT="${MOUNT_POINT:-/media/milan-roy/test_ext4}"

read -rp "  Size per run, suffix k/m/g required (default: 10g): " SIZE
SIZE="${SIZE:-10g}"

read -rp "  Block sizes, space-separated, suffix k/m/g required (default: 128k 256k 512k 1m): " BS_INPUT
BS_INPUT="${BS_INPUT:-128k 256k 512k 1m}"
read -ra BLOCK_SIZES <<< "$BS_INPUT"

read -rp "  Number of repeated runs N (default: 5): " N_INPUT
N_INPUT="${N_INPUT:-5}"
if ! [[ "$N_INPUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: N must be a positive integer."
    exit 1
fi
N=$N_INPUT

echo ""
echo "  Configuration summary:"
echo "    Config label : $CONFIG"
echo "    Device       : $DEVICE"
echo "    Mount point  : $MOUNT_POINT"
echo "    Size         : $SIZE"
echo "    Block sizes  : ${BLOCK_SIZES[*]}"
echo "    Runs (N)     : $N"
echo ""
read -rp "  Proceed? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_FILE="$PROJECT_DIR/seq_write_workload.fio"
JOURNAL_SIZE_MB=64

# ── Prerequisite checks ───────────────────────────────────────────────────────
echo ""
echo "[1/5] Checking prerequisites..."

if [ ! -b "$DEVICE" ]; then
    echo "ERROR: Block device $DEVICE not found."
    echo "       Verify the partition exists with: lsblk"
    exit 1
fi

if [ ! -f "$WORKLOAD_FILE" ]; then
    echo "ERROR: Workload file not found at $WORKLOAD_FILE"
    exit 1
fi

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

if ! command -v bpftrace &> /dev/null; then
    echo "bpftrace not found. Installing..."
    apt-get install -y bpftrace
else
    echo "  bpftrace:      OK ($(bpftrace --version 2>&1))"
fi

ENABLE_JBD2_TRACE=1
JBD2_TRACE_SCRIPT=""
if [ "$ENABLE_JBD2_TRACE" = "1" ]; then
    if [ ! -f "$PROJECT_DIR/trace_jbd2.bt" ]; then
        echo "WARNING: trace_jbd2.bt not found -- JBD2 tracing disabled."
    else
        JBD2_TRACE_SCRIPT="$PROJECT_DIR/trace_jbd2.bt"
        # Compute dev_t for the device: (MAJOR << 20) | MINOR
        DEV_MAJOR=$(printf '%d' "0x$(stat -c '%t' "$DEVICE" 2>/dev/null)")
        DEV_MINOR=$(printf '%d' "0x$(stat -c '%T' "$DEVICE" 2>/dev/null)")
        DEV_T=$(( (DEV_MAJOR << 20) | DEV_MINOR ))
        echo "  dev_t:         $DEV_T  (major=$DEV_MAJOR minor=$DEV_MINOR)"
    fi
fi

echo "  device:        OK ($DEVICE)"
echo "  workload file: OK ($WORKLOAD_FILE)"

# ── Ensure debugfs is mounted ─────────────────────────────────────────────────
if ! mountpoint -q /sys/kernel/debug; then
    mount -t debugfs none /sys/kernel/debug
    echo "  debugfs mounted at /sys/kernel/debug"
else
    echo "  debugfs:       OK (already mounted)"
fi

# ── Verify NVMe partition ─────────────────────────────────────────────────────
echo ""
echo "[2/5] Verifying NVMe partition..."
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

    RESULTS_DIR="$PROJECT_DIR/results/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$RESULTS_DIR"
    echo "  Results directory: $RESULTS_DIR"
    RUN_DIRS+=("$RESULTS_DIR")

    echo ""
    echo "[5/5] Running sequential write workload: modes × block sizes..."
    echo "      Modes: ordered / journal / writeback / nojournal"
    echo "      Block sizes: ${BLOCK_SIZES[*]}"
    echo ""

    for MODE in ordered journal writeback nojournal; do
        for BS in "${BLOCK_SIZES[@]}"; do

            echo "------------------------------------------------------------"
            echo "  Mode: $MODE  |  BS: $BS"
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
            echo "  Starting benchmark: size=$SIZE bs=$BS ..."
            echo ""

            # ── Start blktrace ────────────────────────────────────────────────
            sleep 0.5
            BLKTRACE_OUT="$RESULTS_DIR/blktrace_${MODE}_bs=${BS}"
            blktrace -d "$DEVICE" -D "$RESULTS_DIR" -o "blktrace_${MODE}_bs=${BS}" &
            BLKTRACE_PID=$!
            sleep 0.5

            # ── Start bpftrace JBD2 tracer ────────────────────────────────────
            BPFTRACE_PID=""
            if [ -n "$JBD2_TRACE_SCRIPT" ]; then
                bpftrace "$JBD2_TRACE_SCRIPT" "$DEV_T" \
                    > "$RESULTS_DIR/jbd2_trace_${MODE}_bs=${BS}.txt" 2>&1 &
                BPFTRACE_PID=$!
                sleep 0.5
                echo "  bpftrace JBD2 tracer started (PID $BPFTRACE_PID)"
            fi

            # ── Run fio ───────────────────────────────────────────────────────
            fio "$WORKLOAD_FILE" \
                --directory="$MOUNT_POINT" \
                --bs="$BS" \
                --size="$SIZE" \
                --output-format=json \
                --output="$RESULTS_DIR/sync_heavy_${MODE}_bs=${BS}.json" || {
                echo "ERROR: fio failed for mode '$MODE' bs='$BS' (exit code $?)."
                echo "       Check: df -h $MOUNT_POINT"
                kill "$BLKTRACE_PID" 2>/dev/null
                wait "$BLKTRACE_PID" 2>/dev/null
                [ -n "$BPFTRACE_PID" ] && kill "$BPFTRACE_PID" 2>/dev/null
                [ -n "$BPFTRACE_PID" ] && wait "$BPFTRACE_PID" 2>/dev/null
                umount "$MOUNT_POINT"
                exit 1
            }

            sync -f "$MOUNT_POINT"

            # ── Stop bpftrace ─────────────────────────────────────────────────
            if [ -n "$BPFTRACE_PID" ]; then
                kill "$BPFTRACE_PID" 2>/dev/null
                wait "$BPFTRACE_PID" 2>/dev/null
                echo "  bpftrace: JBD2 trace saved to jbd2_trace_${MODE}_bs=${BS}.txt"
            fi

            # ── Stop blktrace ─────────────────────────────────────────────────
            kill "$BLKTRACE_PID" 2>/dev/null
            wait "$BLKTRACE_PID" 2>/dev/null

            blkparse -i "$BLKTRACE_OUT" -f "%a %d %N\n" -q 2>/dev/null \
                | awk '$1=="C" && $2~/W/ {sum+=$3} END {print sum+0}' \
                > "$RESULTS_DIR/phys_write_bytes_${MODE}_bs=${BS}.txt"
            echo "  blktrace: physical write bytes saved to phys_write_bytes_${MODE}_bs=${BS}.txt"

            echo ""
            echo "  Mode $MODE bs=$BS complete."
            echo ""

        done
    done

    # ── Restore filesystem after nojournal run ────────────────────────────────
    echo "Restoring journal..."
    umount -l "$MOUNT_POINT"
    tune2fs -O has_journal -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
    mount -o data=ordered "$DEVICE" "$MOUNT_POINT"
    echo "  Journal restored."

    # Pause between runs
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

python3 "$ANALYSIS_SCRIPT" --block-sizes "${BLOCK_SIZES[@]}" --size "$SIZE" -- "${RUN_DIRS[@]}"

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
echo "    python3 analyse_results.py --block-sizes ${BLOCK_SIZES[*]} --size $SIZE -- ${RUN_DIRS[*]}"
