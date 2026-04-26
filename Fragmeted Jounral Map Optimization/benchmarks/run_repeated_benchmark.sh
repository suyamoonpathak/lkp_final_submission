#!/bin/bash
# run_repeated_benchmark.sh
#
# Runs full_benchmark.sh N times (default 5) and prints ONLY the
# Results Comparison table from each run. All other benchmark output
# is silenced. 
#
# Usage: sudo bash run_repeated_benchmark.sh [N]
#   N = number of repetitions (default: 5)

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Run as root. Use: sudo bash run_repeated_benchmark.sh [N]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCHMARK_SCRIPT="$SCRIPT_DIR/full_benchmark.sh"
N="${1:-5}"   # number of repetitions, default 5

if [ ! -f "$BENCHMARK_SCRIPT" ]; then
    echo "ERROR: full_benchmark.sh not found at $BENCHMARK_SCRIPT"
    exit 1
fi

# Directory to store per-run logs (full output saved silently)
LOG_DIR="$SCRIPT_DIR/repeated_run_logs/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR"

echo "============================================================"
echo "  FJM Repeated Benchmark  ($N runs)"
echo "  Full output → $LOG_DIR"
echo "============================================================"

for i in $(seq 1 "$N"); do
    LOG_FILE="$LOG_DIR/run_${i}.log"

    # Run the full benchmark, suppress ALL terminal output, save to log
    bash "$BENCHMARK_SCRIPT" > "$LOG_FILE" 2>&1
    EXIT_CODE=$?

    if [ $EXIT_CODE -ne 0 ]; then
        echo ""
        echo "  ⚠  Run $i/$N FAILED (exit code $EXIT_CODE)."
        echo "     See full log: $LOG_FILE"
        continue
    fi

    # Extract and print ONLY the Results Comparison section
    # (from the [5/5] header line up to but not including "Raw results:")
    echo ""
    echo "══════════════════════════════════════════════════════════"
    echo "  Run $i / $N"
    echo "══════════════════════════════════════════════════════════"
    awk '/^\[5\/5\] Results Comparison/,/^Raw results:/' "$LOG_FILE" \
        | grep -v "^Raw results:" \
        | sed 's/^/  /'
done

echo ""
echo "============================================================"
echo "  All $N runs complete."
echo "  Full logs saved in: $LOG_DIR"
echo "============================================================"
