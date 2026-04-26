#!/bin/bash
set -e

echo "=================================================================="
echo "       JBD2 LOCKLESS OPTIMIZATION: FULL SUITE RUNNER              "
echo "=================================================================="
echo "This script natively triggers your Ext4 optimization validation"
echo "and full statistical benchmark structure seamlessly."
echo ""

# Request topological bounds once centrally
read -p "Enter Target Device [default: /dev/nvme0n1p7]: " TARGET_DEV
TARGET_DEV=${TARGET_DEV:-/dev/nvme0n1p7}

read -p "Enter Mount Path [default: /mnt/analysis_2]: " TARGET_MNT
TARGET_MNT=${TARGET_MNT:-/mnt/analysis_2}

echo ""
echo "[1/2] Executing Formal Ext4 Crash-Consistency & Journal Validation..."
# Stdin bypasses the internal prompts to execute frictionlessly
echo -e "$TARGET_DEV\n$TARGET_MNT" | ./validation.sh

echo ""
echo "[2/2] Executing Full 4x Benchmark Scalability Matrix (I/O Stress)..."
echo -e "$TARGET_DEV\n$TARGET_MNT" | ./run_evals.sh

echo "=================================================================="
echo "      Master Execution Completed Successfully!                    "
echo "=================================================================="
echo "Generating Final Array Visualization Graphics..."
python3 plot_results.py

echo "Done! The testing structure is fully archived."
echo "Check 'optimization_results.png' directly for your final graphics!"
