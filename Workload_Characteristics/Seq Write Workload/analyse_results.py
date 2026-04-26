#!/usr/bin/env python3
"""
analyse_results.py

Parses fio JSON, blktrace, and bpftrace JBD2 output from sync-heavy benchmarks.
Supports multiple block sizes and N repeated runs (averaged with stddev).

Called automatically by run_analysis.sh, or run manually:

  Usage:
    python3 analyse_results.py --block-sizes 4k 8k 16k 32k 64k \\
                                   --size 100m -- <results_dir> [<dir2> ...]

  Single run:
    python3 analyse_results.py --block-sizes 4k 8k -- results/20260422_120000

  Multiple (averaged) runs:
    python3 analyse_results.py --block-sizes 4k 8k \\
        -- results/20260422_120000 results/20260422_130000
"""

import argparse
import json
import math
import os
import re
import sys

MODES = ["ordered", "journal", "writeback", "nojournal"]


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_size_mb(size_str):
    """Convert fio size string (e.g. '100m', '1g') to MB float."""
    s = size_str.strip().lower()
    if s.endswith('g'):
        return float(s[:-1]) * 1024
    if s.endswith('m'):
        return float(s[:-1])
    if s.endswith('k'):
        return float(s[:-1]) / 1024
    return float(s) / (1024 * 1024)


# ── Per-run data loading ──────────────────────────────────────────────────────

def load_fio_metrics(results_dir, mode, bs):
    path = os.path.join(results_dir, f"sync_heavy_{mode}_bs={bs}.json")
    with open(path) as f:
        data = json.load(f)

    job     = data["jobs"][0]
    write   = job["write"]
    clat_ns = write["clat_ns"]

    sync_block = job.get("sync", {})
    if sync_block and "lat_ns" in sync_block and sync_block["lat_ns"].get("mean", 0) > 0:
        sync_ns = sync_block["lat_ns"]
    else:
        sync_ns = {"mean": 0, "percentile": {k: 0 for k in clat_ns.get("percentile", {})}}

    write_lat_ns = write["lat_ns"]
    write_pct    = write_lat_ns.get("percentile", {})
    sync_pct     = sync_ns.get("percentile", {})
    runtime_s    = job["job_runtime"] / 1000

    return {
        "iops"      : write["iops"],
        "bw_mbs"    : write["bw"] / 1024,
        "logical_mb": write["io_bytes"] / (1024**2),
        "total_ios" : write["total_ios"],
        "iops_min"  : write["iops_min"],
        "iops_max"  : write["iops_max"],
        "iops_stddev": write["iops_stddev"],
        "avg_ms"    : (write_lat_ns["mean"] + sync_ns["mean"]) / 1_000_000,
        "p50_ms"    : (float(write_pct.get("50.000000", 0)) + float(sync_pct.get("50.000000", 0))) / 1_000_000,
        "p99_ms"    : (float(write_pct.get("99.000000", 0)) + float(sync_pct.get("99.000000", 0))) / 1_000_000,
        "write_us"  : clat_ns["mean"] / 1_000,
        "sync_us"   : sync_ns["mean"] / 1_000,
        "runtime_s" : runtime_s,
    }


def load_phys_bytes(results_dir, mode, bs):
    path = os.path.join(results_dir, f"phys_write_bytes_{mode}_bs={bs}.txt")
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return None


def load_jbd2(results_dir, mode, bs):
    path = os.path.join(results_dir, f"jbd2_trace_{mode}_bs={bs}.txt")
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        return None

    m = re.search(
        r'--- JBD2_TRACE_RESULTS_BEGIN ---\n(.*?)--- JBD2_TRACE_RESULTS_END ---',
        content, re.DOTALL
    )
    if not m:
        return None

    parsed = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if '=' in line:
            key, _, val = line.partition('=')
            try:
                parsed[key.strip()] = int(val.strip())
            except ValueError:
                parsed[key.strip()] = 0
    return parsed


def load_run(results_dir, block_sizes):
    """Load all data for one results directory. Returns nested dicts."""
    fio   = {}
    phys  = {}
    jbd2  = {}
    for mode in MODES:
        fio[mode]  = {}
        phys[mode] = {}
        jbd2[mode] = {}
        for bs in block_sizes:
            try:
                fio[mode][bs] = load_fio_metrics(results_dir, mode, bs)
            except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
                print(f"ERROR loading fio data for mode={mode} bs={bs} from {results_dir}: {e}")
                sys.exit(1)
            phys[mode][bs] = load_phys_bytes(results_dir, mode, bs)
            jbd2[mode][bs] = load_jbd2(results_dir, mode, bs)
    return fio, phys, jbd2


# ── Aggregation ───────────────────────────────────────────────────────────────

def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0

def _stddev(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m)**2 for x in vals) / (len(vals) - 1))

def _ms(val):
    """Wrap a scalar into a (mean, stddev=0) pair."""
    return (val, 0.0)


def aggregate_runs(all_runs, block_sizes):
    """
    all_runs: list of (fio, phys, jbd2) dicts from load_run().
    Returns (agg_fio, agg_phys, agg_jbd2) where each leaf is (mean, stddev).
    """
    N = len(all_runs)

    agg_fio  = {mode: {bs: {} for bs in block_sizes} for mode in MODES}
    agg_phys = {mode: {bs: None for bs in block_sizes} for mode in MODES}
    agg_jbd2 = {mode: {bs: None for bs in block_sizes} for mode in MODES}

    for mode in MODES:
        for bs in block_sizes:
            # fio metrics
            keys = all_runs[0][0][mode][bs].keys()
            for key in keys:
                vals = [run[0][mode][bs][key] for run in all_runs]
                agg_fio[mode][bs][key] = (_mean(vals), _stddev(vals))

            # physical bytes
            phys_vals = [run[1][mode][bs] for run in all_runs if run[1][mode][bs] is not None]
            if phys_vals:
                agg_phys[mode][bs] = (_mean(phys_vals), _stddev(phys_vals))

            # jbd2
            jbd2_dicts = [run[2][mode][bs] for run in all_runs if run[2][mode][bs] is not None]
            if jbd2_dicts:
                all_keys = set()
                for d in jbd2_dicts:
                    all_keys.update(d.keys())
                agg_jbd2[mode][bs] = {}
                for key in all_keys:
                    vals = [d.get(key, 0) for d in jbd2_dicts]
                    agg_jbd2[mode][bs][key] = (_mean(vals), _stddev(vals))

    return agg_fio, agg_phys, agg_jbd2


# ── Table printing ────────────────────────────────────────────────────────────

def _fmt(val_pair, fmt=".2f", suffix=""):
    """Format a (mean, stddev) pair, always showing ± notation."""
    m, s = val_pair
    return f"{m:{fmt}}{suffix} ±{s:{fmt}}"

def _fv(val_pair):
    """Return just the mean."""
    return val_pair[0]


def print_table1(agg_fio, agg_phys, agg_jbd2, block_sizes, size_mb, N, W):
    """
    Table 1: Performance metrics
    Columns: Mode, BS, IOPS, BW (MB/s), Runtime (s), Avg Latency (ms), write (us)
    """
    print()
    print("=" * W)
    if N > 1:
        print(f"  TABLE 1: Performance Metrics  (mean ± stddev across {N} runs)")
    else:
        print("  TABLE 1: Performance Metrics")
    print("=" * W)

    col_mode   = 12
    col_bs     = 6
    col_iops   = 10
    col_bw     = 12
    col_rt     = 12
    col_avglat = 14
    col_wus    = 12

    hdr = (f"  {'Mode':<{col_mode}}  {'BS':<{col_bs}}"
           f"  {'IOPS':>{col_iops}}  {'BW (MB/s)':>{col_bw}}"
           f"  {'Runtime(s)':>{col_rt}}  {'AvgLat(ms)':>{col_avglat}}"
           f"  {'write(us)':>{col_wus}}")
    print(hdr)
    print("  " + "-" * (W - 2))

    for mode in MODES:
        for bs in block_sizes:
            m = agg_fio[mode][bs]
            if N > 1:
                iops_str = _fmt(m["iops"], ".0f")
                bw_str   = _fmt(m["bw_mbs"], ".2f")
                rt_str   = _fmt(m["runtime_s"], ".2f")
                al_str   = _fmt(m["avg_ms"], ".2f")
                wr_str   = _fmt(m["write_us"], ".0f")
            else:
                iops_str = f"{_fv(m['iops']):.0f}"
                bw_str   = f"{_fv(m['bw_mbs']):.2f}"
                rt_str   = f"{_fv(m['runtime_s']):.2f}"
                al_str   = f"{_fv(m['avg_ms']):.2f}"
                wr_str   = f"{_fv(m['write_us']):.0f}"

            print(f"  {mode:<{col_mode}}  {bs:<{col_bs}}"
                  f"  {iops_str:>{col_iops}}  {bw_str:>{col_bw}}"
                  f"  {rt_str:>{col_rt}}  {al_str:>{col_avglat}}"
                  f"  {wr_str:>{col_wus}}")

        # Blank separator between modes
        print()


def print_table2(agg_fio, agg_phys, agg_jbd2, block_sizes, size_mb, N, W):
    """
    Table 2: Write Amplification & JBD2 Journal Metrics
    Columns: Mode, BS, Physical(MB), WAF,
             Total Transactions, Runtime(s), Commit Interval(s)
    """
    print()
    print("=" * W)
    if N > 1:
        print(f"  TABLE 2: Write Amplification & JBD2 Journal Metrics  (mean ± stddev across {N} runs)")
    else:
        print("  TABLE 2: Write Amplification & JBD2 Journal Metrics")
    print("=" * W)
    print(f"  WAF = Physical MB / nojournal Physical MB (per block size).")
    print(f"  Commit Interval = Runtime / Total Transactions.")
    print()

    col_mode     = 12
    col_bs       = 6
    col_phys     = 14
    col_waf      = 8
    col_txns     = 18
    col_runtime  = 14
    col_interval = 20

    hdr = (f"  {'Mode':<{col_mode}}  {'BS':<{col_bs}}"
           f"  {'Physical(MB)':>{col_phys}}"
           f"  {'WAF':>{col_waf}}"
           f"  {'Total Txns':>{col_txns}}"
           f"  {'Runtime(s)':>{col_runtime}}  {'Commit Interval(s)':>{col_interval}}")
    print(hdr)
    print("  " + "-" * (W - 2))

    # Pre-compute nojournal physical MB per block size for WAF
    nj_phys_mb = {}
    for bs in block_sizes:
        v = agg_phys["nojournal"][bs]
        nj_phys_mb[bs] = _fv(v) / (1024**2) if v is not None else None

    for mode in MODES:
        for bs in block_sizes:
            # Physical MB
            pv = agg_phys[mode][bs]
            if pv is not None:
                phys_mb_m = pv[0] / (1024**2)
                phys_mb_s = pv[1] / (1024**2)
                if N > 1:
                    phys_str = _fmt((phys_mb_m, phys_mb_s), ".3f")
                else:
                    phys_str = f"{phys_mb_m:.3f}"
            else:
                phys_mb_m = None
                phys_mb_s = 0.0
                phys_str  = "N/A"

            # WAF
            nj_mb = nj_phys_mb.get(bs)
            if phys_mb_m is not None and nj_mb and nj_mb > 0:
                waf_m = phys_mb_m / nj_mb
                if N > 1:
                    nj_pv = agg_phys["nojournal"][bs]
                    nj_mb_s = nj_pv[1] / (1024**2) if nj_pv is not None else 0.0
                    rel_err = math.sqrt((phys_mb_s / phys_mb_m)**2 + (nj_mb_s / nj_mb)**2) \
                              if (phys_mb_m > 0 and nj_mb > 0) else 0.0
                    waf_s = waf_m * rel_err
                    waf_str = _fmt((waf_m, waf_s), ".3f", "x")
                else:
                    waf_str = f"{waf_m:.3f}x"
            else:
                waf_str = "N/A"

            # JBD2 metrics
            jd = agg_jbd2[mode][bs]

            def jg(key):
                if jd is None:
                    return (0, 0)
                return jd.get(key, (0, 0))

            if mode == "nojournal" or jd is None:
                txns_str     = "(disabled)"
                interval_str = "(disabled)"
            else:
                # Total transactions
                txns_m, txns_s = jg("COMMIT_COUNT")
                if N > 1:
                    txns_str = _fmt((txns_m, txns_s), ".0f")
                else:
                    txns_str = f"{txns_m:.0f}"

                # Commit Interval (s) = Runtime (s) / Total Transactions
                rt_m, rt_s = agg_fio[mode][bs]["runtime_s"]
                if txns_m > 0:
                    interval_m = rt_m / txns_m
                    if N > 1:
                        # Propagate uncertainty: I = R/T, sigma_I/I = sqrt((sR/R)^2 + (sT/T)^2)
                        rel_err = math.sqrt((rt_s / rt_m)**2 + (txns_s / txns_m)**2) \
                                  if (rt_m > 0 and txns_m > 0) else 0.0
                        interval_s = interval_m * rel_err
                        interval_str = _fmt((interval_m, interval_s), ".4f")
                    else:
                        interval_str = f"{interval_m:.4f}"
                else:
                    interval_str = "0"

            # Runtime(s) for this mode/bs
            rt_m, rt_s = agg_fio[mode][bs]["runtime_s"]
            if N > 1:
                rt_str = _fmt((rt_m, rt_s), ".2f")
            else:
                rt_str = f"{rt_m:.2f}"

            print(f"  {mode:<{col_mode}}  {bs:<{col_bs}}"
                  f"  {phys_str:>{col_phys}}"
                  f"  {waf_str:>{col_waf}}"
                  f"  {txns_str:>{col_txns}}"
                  f"  {rt_str:>{col_runtime}}  {interval_str:>{col_interval}}")

        print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--block-sizes", nargs="+", default=["128k", "256k", "512k", "1m"],
                        metavar="BS", help="Block sizes to analyse (e.g. 128k 256k 512k 1m)")
    parser.add_argument("--size", default="10g",
                        help="Logical size written per run (e.g. 10g)")
    parser.add_argument("results_dirs", nargs="+", metavar="results_dir",
                        help="One or more results directories to load and average")
    args = parser.parse_args()

    block_sizes = args.block_sizes
    size_mb     = parse_size_mb(args.size)
    run_dirs    = args.results_dirs
    N           = len(run_dirs)

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

    all_runs = []
    for d in run_dirs:
        all_runs.append(load_run(d, block_sizes))

    agg_fio, agg_phys, agg_jbd2 = aggregate_runs(all_runs, block_sizes)

    W = 130

    print()
    print("=" * W)
    hdr_detail = f"1 thread · {args.size} · sequential write · BS: {', '.join(block_sizes)}"
    if N > 1:
        print(f"  SEQUENTIAL WRITE WORKLOAD RESULTS  ({N} runs averaged  |  {hdr_detail})")
    else:
        print(f"  SEQUENTIAL WRITE WORKLOAD RESULTS  ({hdr_detail})")
    print("=" * W)

    print_table1(agg_fio, agg_phys, agg_jbd2, block_sizes, size_mb, N, W)
    print_table2(agg_fio, agg_phys, agg_jbd2, block_sizes, size_mb, N, W)

    print()
    if N > 1:
        print(f"  Values shown as mean ± stddev across {N} runs.")
    print(f"  Raw results: {' '.join(run_dirs)}")
    print()


if __name__ == "__main__":
    main()
