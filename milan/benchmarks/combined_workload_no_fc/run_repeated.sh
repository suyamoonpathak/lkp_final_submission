#!/bin/bash
# run_repeated.sh
#
# Runs run_combined_analysis.sh N times and then calls analyse_averaged_results.py
# with all N result directories to produce mean ± stddev tables.
#
# Usage: sudo bash run_repeated.sh [N]
#   N defaults to 3 if not provided.
#
# Each run stores its results in a separate timestamped subdirectory under
# results/. After all runs, the averaged analysis is printed to the terminal
# and saved to results/averaged_<timestamp>.txt.

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    echo "       Run it with: sudo bash run_repeated.sh [N]"
    exit 1
fi

N=${1:-3}

if ! [[ "$N" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: N must be a positive integer (got: '$N')"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH_SCRIPT="$PROJECT_DIR/run_combined_analysis.sh"
ANALYSIS_SCRIPT="$PROJECT_DIR/analyse_averaged_results.py"
RESULTS_BASE="$PROJECT_DIR/results"

if [ ! -f "$BENCH_SCRIPT" ]; then
    echo "ERROR: Benchmark script not found at $BENCH_SCRIPT"
    exit 1
fi

if [ ! -f "$ANALYSIS_SCRIPT" ]; then
    echo "ERROR: Analysis script not found at $ANALYSIS_SCRIPT"
    exit 1
fi

mkdir -p "$RESULTS_BASE"

echo "============================================================"
echo "  Combined Workload Repeated Benchmark  (N=$N runs)"
echo "  JBD2 Benchmarking -- CS614 IIT Kanpur"
echo "============================================================"
echo ""

RESULT_DIRS=()

for i in $(seq 1 "$N"); do
    echo "------------------------------------------------------------"
    echo "  Run $i / $N"
    echo "------------------------------------------------------------"

    BEFORE=$(find "$RESULTS_BASE" -mindepth 1 -maxdepth 1 -type d | sort)

    bash "$BENCH_SCRIPT"
    BENCH_EXIT=$?

    if [ $BENCH_EXIT -ne 0 ]; then
        echo "ERROR: run_combined_analysis.sh failed on run $i (exit code $BENCH_EXIT)."
        exit 1
    fi

    AFTER=$(find "$RESULTS_BASE" -mindepth 1 -maxdepth 1 -type d | sort)
    NEW_DIR=$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | head -1)

    if [ -z "$NEW_DIR" ]; then
        echo "ERROR: Could not detect new results directory after run $i."
        echo "       Expected a new subdirectory under $RESULTS_BASE"
        exit 1
    fi

    RESULT_DIRS+=("$NEW_DIR")
    echo ""
    echo "  Run $i complete. Results at: $NEW_DIR"
    echo ""

    if [ $i -lt "$N" ]; then
        echo "  Sleeping 5s before next run..."
        sleep 5
    fi
done

echo "============================================================"
echo "  All $N runs complete."
echo "  Result directories:"
for d in "${RESULT_DIRS[@]}"; do
    echo "    $d"
done
echo "============================================================"
echo ""
echo "  Running averaged analysis..."
echo ""

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_FILE="$RESULTS_BASE/averaged_${TIMESTAMP}.txt"

python3 "$ANALYSIS_SCRIPT" "${RESULT_DIRS[@]}" | tee "$OUTPUT_FILE"

echo ""
echo "Averaged results saved to: $OUTPUT_FILE"
echo ""
echo "To re-run the averaged analysis on these directories without re-benchmarking:"
echo "  python3 analyse_averaged_results.py ${RESULT_DIRS[*]}"
