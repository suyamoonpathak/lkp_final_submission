#!/bin/bash
# run_repeated.sh
#
# Runs run_wal_analysis.sh N times. Each run saves its results into a
# separate timestamped subdirectory under results/. After all runs complete,
# calls analyse_averaged_results.py to average across all N result sets
# and print a consolidated comparison table.
#
# Usage: sudo bash run_repeated.sh <N>
# Example: sudo bash run_repeated.sh 5

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    echo "       Run it with: sudo bash run_repeated.sh <N>"
    exit 1
fi

if [ -z "$1" ] || ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: Please provide a positive integer for the number of runs."
    echo "       Usage: sudo bash run_repeated.sh <N>"
    exit 1
fi

N=$1
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH_SCRIPT="$PROJECT_DIR/run_wal_analysis.sh"
ANALYSIS_SCRIPT="$PROJECT_DIR/analyse_averaged_results.py"

if [ ! -f "$BENCH_SCRIPT" ]; then
    echo "ERROR: run_wal_analysis.sh not found at $BENCH_SCRIPT"
    exit 1
fi

if [ ! -f "$ANALYSIS_SCRIPT" ]; then
    echo "ERROR: analyse_averaged_results.py not found at $ANALYSIS_SCRIPT"
    exit 1
fi

echo "============================================================"
echo "  WAL Repeated Benchmark Runner"
echo "  Runs: $N"
echo "  Project: $PROJECT_DIR"
echo "============================================================"
echo ""

RUN_DIRS=()

for i in $(seq 1 "$N"); do
    echo "############################################################"
    echo "  RUN $i of $N"
    echo "############################################################"
    echo ""

    # run_wal_analysis.sh creates its own timestamped subdirectory under
    # $PROJECT_DIR/results/. We capture which directory it created by
    # noting what exists before and after the run.
    BEFORE=$(find "$PROJECT_DIR/results" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)

    bash "$BENCH_SCRIPT"
    EXIT_CODE=$?

    if [ $EXIT_CODE -ne 0 ]; then
        echo ""
        echo "ERROR: run_wal_analysis.sh failed on run $i (exit code $EXIT_CODE). Aborting."
        exit 1
    fi

    AFTER=$(find "$PROJECT_DIR/results" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)

    # The new directory is whatever appeared in AFTER that was not in BEFORE.
    NEW_DIR=$(comm -13 <(echo "$BEFORE") <(echo "$AFTER") | head -1)

    if [ -z "$NEW_DIR" ]; then
        echo "ERROR: Could not determine result directory created by run $i."
        exit 1
    fi

    echo ""
    echo "  Run $i complete. Results saved to: $NEW_DIR"
    echo ""

    RUN_DIRS+=("$NEW_DIR")

    # Brief pause between runs to let the device and caches settle.
    if [ $i -lt $N ]; then
        echo "  Waiting 5 seconds before next run..."
        sleep 5
        echo ""
    fi
done

echo "############################################################"
echo "  All $N runs complete."
echo "############################################################"
echo ""
echo "  Result directories:"
for dir in "${RUN_DIRS[@]}"; do
    echo "    $dir"
done
echo ""

echo "============================================================"
echo "  AVERAGED ANALYSIS: Calling analyse_averaged_results.py..."
echo "============================================================"
echo ""

python3 "$ANALYSIS_SCRIPT" "${RUN_DIRS[@]}"
