#!/usr/bin/env python3
"""
analyse_results.py

Parses fio JSON results from the combined workload benchmark (sync-heavy and
seq-write run simultaneously) and prints a single comparison table, averaged
across N runs with mean ± stddev.

Called automatically by run_analysis.sh, or run manually:

  Usage:
    python3 analyse_results.py --bs-sync 4k --bs-seq 128k -- <dir1> [<dir2> ...]
"""

import argparse
import json
import math
import os
import sys

MODES = ["ordered", "journal", "writeback", "nojournal"]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_fio(results_dir, mode, workload):
    """Load fio JSON for one mode/workload. workload is 'sync' or 'seq'."""
    path = os.path.join(results_dir, f"combined_{mode}_{workload}.json")
    with open(path) as f:
        data = json.load(f)

    job   = data["jobs"][0]
    write = job["write"]

    sync_block = job.get("sync", {})
    if sync_block and "lat_ns" in sync_block and sync_block["lat_ns"].get("mean", 0) > 0:
        sync_ns_mean = sync_block["lat_ns"]["mean"]
    else:
        sync_ns_mean = 0

    write_lat_mean = write["lat_ns"]["mean"]

    return {
        "iops"     : write["iops"],
        "bw_mbs"   : write["bw"] / 1024,
        "io_bytes" : write["io_bytes"],
        "avg_ms"   : (write_lat_mean + sync_ns_mean) / 1_000_000,
    }


def load_run(results_dir):
    run = {}
    for mode in MODES:
        run[mode] = {}
        for wl in ("sync", "seq"):
            try:
                run[mode][wl] = load_fio(results_dir, mode, wl)
            except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
                print(f"ERROR loading {wl} results for mode={mode} from {results_dir}: {e}")
                sys.exit(1)
    return run


# ── Aggregation ───────────────────────────────────────────────────────────────

def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0

def _stddev(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m)**2 for x in vals) / (len(vals) - 1))

def _fv(pair):
    return pair[0]

def _fmt(pair, fmt=".2f", suffix=""):
    m, s = pair
    return f"{m:{fmt}}{suffix} ±{s:{fmt}}"


def aggregate(all_runs):
    """Average metrics across N runs. Returns {mode: {wl: {key: (mean, stddev)}}}."""
    agg = {mode: {wl: {} for wl in ("sync", "seq")} for mode in MODES}
    for mode in MODES:
        for wl in ("sync", "seq"):
            for key in all_runs[0][mode][wl].keys():
                vals = [run[mode][wl][key] for run in all_runs]
                agg[mode][wl][key] = (_mean(vals), _stddev(vals))
    return agg


# ── Table printing ────────────────────────────────────────────────────────────

def print_table(agg, bs_sync, bs_seq, N, W):
    print()
    print("=" * W)
    if N > 1:
        print(f"  COMBINED WORKLOAD RESULTS  (mean ± stddev across {N} runs)")
    else:
        print("  COMBINED WORKLOAD RESULTS")
    print("=" * W)
    print(f"  SYNC: sync-heavy workload (fsync after every write)  BS={bs_sync}")
    print(f"  SEQ:  sequential write workload (no fsync)           BS={bs_seq}")
    print()

    col_wl   = 8
    col_mode = 12
    col_iops = 10
    col_bw   = 12
    col_data = 16
    col_lat  = 14

    hdr = (f"  {'Workload':<{col_wl}}  {'Mode':<{col_mode}}"
           f"  {'IOPS':>{col_iops}}  {'BW (MB/s)':>{col_bw}}"
           f"  {'Data Written':>{col_data}}  {'AvgLat (ms)':>{col_lat}}")
    print(hdr)
    print("  " + "-" * (W - 2))

    for wl, label in (("sync", "SYNC"), ("seq", "SEQ")):
        for mode in MODES:
            m = agg[mode][wl]

            data_gb_m = _fv(m["io_bytes"]) / (1024**3)
            data_gb_s = m["io_bytes"][1]   / (1024**3)

            if N > 1:
                iops_str = _fmt(m["iops"],              ".0f")
                bw_str   = _fmt(m["bw_mbs"],            ".2f")
                data_str = _fmt((data_gb_m, data_gb_s), ".3f", " GB")
                lat_str  = _fmt(m["avg_ms"],            ".2f")
            else:
                iops_str = f"{_fv(m['iops']):.0f}"
                bw_str   = f"{_fv(m['bw_mbs']):.2f}"
                data_str = f"{data_gb_m:.3f} GB"
                lat_str  = f"{_fv(m['avg_ms']):.2f}"

            print(f"  {label:<{col_wl}}  {mode:<{col_mode}}"
                  f"  {iops_str:>{col_iops}}  {bw_str:>{col_bw}}"
                  f"  {data_str:>{col_data}}  {lat_str:>{col_lat}}")

        print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bs-sync", default="8k",
                        help="Block size used for sync-heavy workload (e.g. 8k)")
    parser.add_argument("--bs-seq",  default="1m",
                        help="Block size used for seq-write workload (e.g. 1m)")
    parser.add_argument("results_dirs", nargs="+", metavar="results_dir",
                        help="One or more results directories to load and average")
    args = parser.parse_args()

    run_dirs = args.results_dirs
    N = len(run_dirs)

    for d in run_dirs:
        if not os.path.isdir(d):
            print(f"ERROR: Results directory not found: {d}")
            sys.exit(1)

    print()
    if N > 1:
        print(f"  Loading results from {N} run(s):")
        for d in run_dirs:
            print(f"    {d}")
    else:
        print(f"  Results directory: {run_dirs[0]}")

    all_runs = [load_run(d) for d in run_dirs]
    agg = aggregate(all_runs)

    W = 90

    print_table(agg, args.bs_sync, args.bs_seq, N, W)

    if N > 1:
        print(f"  Values shown as mean ± stddev across {N} runs.")
    print(f"  Raw results: {' '.join(run_dirs)}")
    print()


if __name__ == "__main__":
    main()
