#!/bin/bash
# run_wal_analysis.sh
#
# WAL benchmarking script for JBD2 journaling mode comparison.
# This script handles only the benchmarking phase:
#   1. Checks prerequisites (fio, device, workload file)
#   2. Verifies the target NVMe partition
#   3. Mounts the filesystem
#   4. Runs the WAL workload across all three journaling modes
#   5. Cleans up between runs for fair comparison
#   6. Calls analyse_wal_results.py to parse and display results
#
# Analysis is handled by a separate script:
#   scripts/analyse_wal_results.py
# You can re-run analysis on saved results without re-benchmarking:
#   python3 scripts/analyse_wal_results.py results/wal/<timestamp>
#
# Prerequisites:
#   - /dev/nvme0n1p6 exists and is already formatted as ext4
#   - The companion workload file exists at workloads/wal_workload.fio
#
# Usage: sudo bash scripts/run_wal_analysis.sh

# ── Safety check: must run as root ───────────────────────────────────────────
# Mounting filesystems, writing to /sys, and dropping caches all require root.
# We check this at the very start so we fail immediately with a clear message
# rather than failing silently halfway through the script.
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    echo "       Run it with: sudo bash scripts/run_wal_analysis.sh"
    exit 1
fi

# ── Configuration variables ───────────────────────────────────────────────────
# Centralising these at the top means you only need to change one place
# if your paths differ from the defaults.
DEVICE="/dev/nvme0n1p6"
MOUNT_POINT="/media/milan-roy/test_ext4"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_FILE="$PROJECT_DIR/wal_workload.fio"
JOURNAL_SIZE_MB=64  # ext4 journal size in MB (min 4, max 10240)

# Results go into a timestamped directory so multiple runs don't overwrite
# each other. You can compare results from different sessions later.
RESULTS_DIR="$PROJECT_DIR/results/$(date +%Y%m%d_%H%M%S)"

# ── Feature flags ─────────────────────────────────────────────────────────────
# Set to 1 to enable, 0 to disable.
ENABLE_JBD2_TRACE=1   # Run bpftrace JBD2 tracing alongside each benchmark mode

# ENABLE_FAST_COMMIT=1: enable ext4 fast commits on the benchmarked partition.
# Fast commits reduce journal commit overhead by writing incremental diffs
# instead of full transaction blocks, lowering per-commit latency and WAF.
# When enabled, tune2fs adds the fast_commit feature flag before each journaled
# mode run and mounts with the commit=1 option to keep commit intervals tight.
# Set to 0 to benchmark standard ext4 journaling without fast commits (default).
ENABLE_FAST_COMMIT=0

# ── Prerequisite checks ───────────────────────────────────────────────────────
# These checks catch common problems before the benchmark starts, rather than
# letting them cause confusing failures midway through.
echo "============================================================"
echo "  WAL Journaling Mode Analysis"
echo "  JBD2 Benchmarking -- CS614 IIT Kanpur"
echo "============================================================"
echo ""
echo "[1/5] Checking prerequisites..."

# Check the NVMe partition exists as a block device.
if [ ! -b "$DEVICE" ]; then
    echo "ERROR: Block device $DEVICE not found."
    echo "       Verify the partition exists with: lsblk"
    exit 1
fi

# Check the workload file exists. Without it, fio has nothing to run.
if [ ! -f "$WORKLOAD_FILE" ]; then
    echo "ERROR: Workload file not found at $WORKLOAD_FILE"
    echo "       Save wal_workload.fio to your home directory first."
    exit 1
fi

# Check fio is installed.
if ! command -v fio &> /dev/null; then
    echo "fio not found. Installing..."
    apt-get install -y fio
    echo "fio installed successfully."
else
    echo "  fio:           OK ($(fio --version))"
fi

# Check blktrace is installed.
if ! command -v blktrace &> /dev/null; then
    echo "blktrace not found. Installing..."
    apt-get install -y blktrace
    echo "blktrace installed successfully."
else
    echo "  blktrace:      OK ($(blktrace --version 2>&1 | head -1))"
fi

# Check bpftrace is installed (needed for JBD2 function call tracing).
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

echo "  device:        OK ($DEVICE)"
echo "  workload file: OK ($WORKLOAD_FILE)"

# ── Step 1: Verify the NVMe partition ────────────────────────────────────────
# We are benchmarking directly on a dedicated NVMe partition rather than
# a loop-device-backed image. This eliminates the double-journaling overhead
# that occurs when the loop file lives on another ext4 filesystem, giving
# cleaner and more representative results.
echo ""
echo "[2/5] Verifying NVMe partition..."
echo "  Device: $DEVICE"
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "$DEVICE" 2>/dev/null || true

# ── Step 2: Mount the filesystem ─────────────────────────────────────────────
# If the partition is already mounted (e.g. auto-mounted by the desktop
# environment), unmount it first so we can remount with explicit journaling
# mode options. We always start with ordered as the baseline.
echo ""
echo "[3/5] Mounting filesystem..."

mkdir -p "$MOUNT_POINT"

# Unmount if currently mounted (e.g. auto-mounted by desktop environment),
# then remount with explicit journaling options.
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

# Show the journal size so it's on record in the output.
JOURNAL_SIZE=$(dumpe2fs "$DEVICE" 2>/dev/null | grep "Total journal size" | awk '{print $NF}')
echo "  Journal size:  ${JOURNAL_SIZE:-unknown}"

# ── Step 3: Prepare results directory ────────────────────────────────────────
echo ""
echo "[4/5] Setting up results directory..."
mkdir -p "$RESULTS_DIR"
echo "  Results will be saved to: $RESULTS_DIR"

# ── Ensure debugfs is mounted ─────────────────────────────────────────────────
# blktrace writes per-CPU trace files through /sys/kernel/debug/block/.
# Without debugfs mounted, blktrace fails with "Operation not permitted".
if ! mountpoint -q /sys/kernel/debug; then
    mount -t debugfs none /sys/kernel/debug
    echo "  debugfs mounted at /sys/kernel/debug"
else
    echo "  debugfs:       OK (already mounted)"
fi

# ── Step 4: Run the benchmark loop ───────────────────────────────────────────
# This is the core of the script. We iterate over all three journaling modes,
# run the WAL workload in each, and save the results as JSON.
#
# The three modes represent the full spectrum of ext4 journaling options:
#   ordered   - metadata journaled, data written before metadata commit (default)
#   journal   - both data and metadata journaled (safest, slowest)
#   writeback - metadata journaled, no ordering guarantee (fastest, least safe)
echo ""
echo "[5/5] Running WAL workload across all four modes..."
echo "      ordered / journal / writeback: full fio JSON + blktrace"
echo "      nojournal: blktrace only (WAF baseline, no analysis output needed)"
echo "      Time varies by mode (journal mode slowest)."
echo ""

for MODE in ordered journal writeback nojournal; do
# for MODE in nojournal; do


    echo "------------------------------------------------------------"
    echo "  Running mode: $MODE"
    echo "------------------------------------------------------------"

    # ── Between-run cleanup ───────────────────────────────────────────────────
    # Delete all files in the partition and remount with the correct journal
    # mode. This resets file content without reformatting, so the filesystem
    # structure (journal size, inode table) stays consistent across modes.
    echo "  Cleaning partition for fresh state..."
    find "$MOUNT_POINT" -mindepth 1 -delete
    umount -l "$MOUNT_POINT"

    if [ "$MODE" = "nojournal" ]; then
        # Disable the journal so JBD2 does not run at all.
        # This gives the physical-write baseline for WAF isolation.
        # fast_commit requires has_journal, so it is always stripped here.
        tune2fs -O ^has_journal,^fast_commit "$DEVICE" &>/dev/null
        mount "$DEVICE" "$MOUNT_POINT"
    elif [ "$MODE" = "ordered" ] && [ "$ENABLE_FAST_COMMIT" = "1" ]; then
        # Fast commit is only valid with data=ordered. It relies on data blocks
        # being written to their final locations before the metadata commit, which
        # is exactly the guarantee ordered mode provides. For journal and writeback
        # the kernel ignores the fast_commit flag and falls back to full commits,
        # so we only enable it here.
        tune2fs -O has_journal,fast_commit -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
        mount -o data=ordered,commit=1 "$DEVICE" "$MOUNT_POINT"
    else
        # journal and writeback modes, or ordered with fast commit disabled:
        # always use standard full commits.
        tune2fs -O has_journal,^fast_commit -J size=$JOURNAL_SIZE_MB "$DEVICE" &>/dev/null
        mount -o data=$MODE "$DEVICE" "$MOUNT_POINT"
    fi
    echo "  Partition formatted and mounted in $MODE mode$([ "$MODE" = "ordered" ] && [ "$ENABLE_FAST_COMMIT" = "1" ] && echo " (fast_commit)")."
    # Pre-create test files without fdatasync so file creation metadata overhead
    # is excluded from the timed measurement. Without this step, journal mode
    # is unfairly penalised during the first pass because it journals data
    # writes too, while writeback only journals metadata -- skewing the relative
    # numbers before steady-state I/O even begins.
    # echo "  Pre-creating test files (one-shot write, no fdatasync)..."
    # fio "$WORKLOAD_FILE" \
    #     --directory="$MOUNT_POINT" \
    #     --time_based=0 \
    #     --fdatasync=0 \
    #     --output-format=normal \
    #     --output=/dev/null
    # sync

    # Drop the page cache, dentry cache, and inode cache. We do this AFTER
    # the pre-create pass so we begin the timed run in the coldest possible
    # state against pre-existing files (pure overwrite, true steady-state WAL).
    echo "  Dropping page cache..."
    echo 3 > /proc/sys/vm/drop_caches

    # Confirm the mode switch actually worked. Never trust that a remount
    # succeeded without checking -- a silent failure here means you would be
    # benchmarking the wrong mode and not know it.
    # NOTE: 'data=ordered' is the kernel default and is often omitted from
    # /proc/mounts even when explicitly set. findmnt is more reliable here.
    if [ "$MODE" = "nojournal" ]; then
        echo "  Active mode confirmed: no-journal (JBD2 disabled, WAF baseline only)"
    else
        ACTIVE_MODE=$(findmnt -no OPTIONS "$MOUNT_POINT" | tr ',' '\n' | grep '^data=')
        [ -z "$ACTIVE_MODE" ] && ACTIVE_MODE="data=ordered (kernel default, not shown in mounts)"
        FC_STATUS=$(tune2fs -l "$DEVICE" 2>/dev/null | grep "Filesystem features" | grep -o "fast_commit" || true)
        [ -n "$FC_STATUS" ] && ACTIVE_MODE="$ACTIVE_MODE fast_commit=on" || ACTIVE_MODE="$ACTIVE_MODE fast_commit=off"
        echo "  Active mode confirmed: $ACTIVE_MODE"
    fi
    echo "  Starting 100 MB benchmark run..."
    echo ""

    # ── Start blktrace ────────────────────────────────────────────────────────
    # Trace the partition (/dev/nvme0n1p6) directly. The kernel's block trace
    # infrastructure filters events to only the sector range of this partition,
    # excluding root filesystem and other partition I/O from the WAF count.
    # fio data writes, journal descriptor/commit blocks, and checkpoint writes
    # all land within p6, so they are all captured correctly.
    # blktrace runs in the background; we stop it precisely after fio finishes
    # so the trace window matches the timed benchmark exactly.
    # The pre-create stage above is intentionally excluded from the trace.
    # Use -D for the output directory and -o for the filename prefix separately.
    # blktrace prepends "./" to whatever is passed to -o, so giving it an
    # absolute path produces ".//absolute/path" which fails to open.
    # Splitting into -D dir + -o name avoids this.
    BLKTRACE_OUT="$RESULTS_DIR/blktrace_${MODE}"
    blktrace -d /dev/nvme0n1p6 -D "$RESULTS_DIR" -o "blktrace_${MODE}" &
    BLKTRACE_PID=$!
    sleep 0.5   # allow blktrace to initialise before fio starts

    # ── Start bpftrace JBD2 tracer ────────────────────────────────────────────
    # Attaches to all 21 JBD2 tracepoints, filtered to nvme0n1p6 via dev_t.
    # In nojournal mode JBD2 is disabled; bpftrace attaches but collects zero
    # events and emits all-zero counters when killed.
    BPFTRACE_PID=""
    if [ -n "$JBD2_TRACE_SCRIPT" ]; then
        bpftrace "$JBD2_TRACE_SCRIPT" \
            > "$RESULTS_DIR/jbd2_trace_${MODE}.txt" 2>&1 &
        BPFTRACE_PID=$!
        sleep 0.5   # allow bpftrace to attach all probes before fio starts
        echo "  bpftrace JBD2 tracer started (PID $BPFTRACE_PID)"
    fi

    # ── Run fio ───────────────────────────────────────────────────────────────
    # --output-format=json saves all metrics in a structured format that
    # the analysis section below can parse programmatically. This includes
    # not just IOPS and bandwidth but every latency percentile bucket.
    #
    # --directory overrides the directory setting in the .fio file to ensure
    # fio writes to our specific mount point regardless of what the .fio
    # file specifies. This makes the .fio file reusable across different setups.
    echo "Using workload file: $WORKLOAD_FILE"

    fio "$WORKLOAD_FILE" \
        --directory="$MOUNT_POINT" \
        --output-format=json \
        --output="$RESULTS_DIR/wal_${MODE}.json" || {
        echo "ERROR: fio failed for mode '$MODE' (exit code $?). Likely out of disk space."
        echo "       Check: df -h $MOUNT_POINT"
        kill "$BLKTRACE_PID" 2>/dev/null
        wait "$BLKTRACE_PID" 2>/dev/null
        [ -n "$BPFTRACE_PID" ] && kill "$BPFTRACE_PID" 2>/dev/null
        [ -n "$BPFTRACE_PID" ] && wait "$BPFTRACE_PID" 2>/dev/null
        umount "$MOUNT_POINT"
        exit 1
    }

    # if [ "$MODE" = "nojournal" ]; then
    #     # Without journaling, metadata (inode, bitmap, group descriptor) stays
    #     # dirty in the page cache after fdatasync -- only data pages are flushed.
    #     # sync -f flushes only this filesystem's dirty pages before blktrace
    #     # stops, so the baseline captures metadata writes too. Using sync -f
    #     # instead of plain sync limits the flush to p6 only, avoiding noise
    #     # from other processes' dirty pages on other partitions.
    #     sync -f "$MOUNT_POINT"
    # fi
    sync -f "$MOUNT_POINT"
    
    # ── Stop bpftrace ─────────────────────────────────────────────────────────
    # Sending SIGINT causes bpftrace to run its END block, flushing all
    # accumulated maps to stdout (redirected to jbd2_trace_${MODE}.txt).
    # wait ensures the END block has fully written before we continue.
    if [ -n "$BPFTRACE_PID" ]; then
        kill "$BPFTRACE_PID" 2>/dev/null
        wait "$BPFTRACE_PID" 2>/dev/null
        echo "  bpftrace: JBD2 trace saved to jbd2_trace_${MODE}.txt"
    fi

    # ── Stop blktrace and compute physical write bytes ────────────────────────
    # Kill blktrace now that fio has finished. wait ensures all trace buffers
    # are flushed to disk before we run blkparse on them.
    kill "$BLKTRACE_PID" 2>/dev/null
    wait "$BLKTRACE_PID" 2>/dev/null

    # blkparse reads all per-CPU trace files (blktrace_${MODE}.blktrace.0, .1, ...)
    # automatically when given the base name. We filter for:
    #   action == D  (dispatched to the device driver -- actual physical I/O)
    #   rwbs   ~  W  (write operation, excludes reads, discards, flushes)
    # and sum the byte counts to get total physical bytes written to the device.
    blkparse -i "$BLKTRACE_OUT" -f "%a %d %N\n" -q 2>/dev/null \
        | awk '$1=="C" && $2~/W/ {sum+=$3} END {print sum+0}' \
        > "$RESULTS_DIR/phys_write_bytes_${MODE}.txt"
    echo "  blktrace: physical write bytes saved to phys_write_bytes_${MODE}.txt"

    echo ""
    echo "  Mode $MODE complete. Results saved to wal_${MODE}.json"
    echo ""

done

# ── Restore filesystem after nojournal baseline run ───────────────────────────
# The nojournal run disabled the journal via tune2fs. Re-enable it so the
# filesystem is left in a healthy journaled state.
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
# All three benchmark runs are done. Hand off to the dedicated analysis
# script which parses the JSON files and prints all comparison tables.
echo "============================================================"
echo "  ANALYSIS: Calling analyse_wal_results.py..."
echo "============================================================"

ANALYSIS_SCRIPT="$PROJECT_DIR/analyse_wal_results.py"
if [ ! -f "$ANALYSIS_SCRIPT" ]; then
    echo "ERROR: Analysis script not found at $ANALYSIS_SCRIPT"
    echo "       Raw JSON results are saved in: $RESULTS_DIR"
    umount "$MOUNT_POINT"
    exit 1
fi

python3 "$ANALYSIS_SCRIPT" "$RESULTS_DIR"

# ── Cleanup: unmount the filesystem ──────────────────────────────────────────
# We unmount at the end to leave the system in a clean state.
# This also triggers a final journal checkpoint, ensuring all data is
# safely written to its permanent locations on disk.
echo ""
echo "Unmounting filesystem..."
umount "$MOUNT_POINT"
echo ""
echo "Done. To re-run the analysis on saved results without re-benchmarking:"
echo "  python3 scripts/analyse_wal_results.py $RESULTS_DIR"
