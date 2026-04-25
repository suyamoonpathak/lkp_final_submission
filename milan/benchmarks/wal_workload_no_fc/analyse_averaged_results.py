#!/usr/bin/env python3
"""
analyse_averaged_results.py

Loads results from N separate WAL benchmark run directories, averages every
metric across all runs, and prints a consolidated comparison table showing
mean ± stddev for each mode and metric.

Called automatically by run_repeated.sh, or run manually:

  Usage:   python3 analyse_averaged_results.py <dir1> <dir2> ... <dirN>
  Example: python3 analyse_averaged_results.py results/20260422_120000 results/20260422_130000
"""

import json
import math
import os
import re
import sys


MODES = ["ordered", "journal", "writeback", "nojournal"]


# ── Per-run data loading (mirrors analyse_wal_results.py) ────────────────────

def load_fio_metrics(results_dir):
    metrics = {}
    for mode in MODES:
        path = os.path.join(results_dir, f"wal_{mode}.json")
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
        disk         = (data.get("disk_util") or [{}])[0]
        runtime_s    = job["job_runtime"] / 1000

        metrics[mode] = {
            "iops"              : write["iops"],
            "bw_mbs"            : write["bw"] / 1024,
            "total_gb"          : write["io_bytes"] / (1024**3),
            "total_ios"         : write["total_ios"],
            "logical_mb"        : write["io_bytes"] / (1024**2),
            "iops_min"          : write["iops_min"],
            "iops_max"          : write["iops_max"],
            "iops_stddev"       : write["iops_stddev"],
            "avg_ms"            : (write_lat_ns["mean"] + sync_ns["mean"]) / 1_000_000,
            "p50_ms"            : (float(write_pct.get("50.000000", 0)) + float(sync_pct.get("50.000000", 0))) / 1_000_000,
            "p99_ms"            : (float(write_pct.get("99.000000", 0)) + float(sync_pct.get("99.000000", 0))) / 1_000_000,
            "p999_ms"           : (float(write_pct.get("99.900000", 0)) + float(sync_pct.get("99.900000", 0))) / 1_000_000,
            "p9990_ms"          : (float(write_pct.get("99.990000", 0)) + float(sync_pct.get("99.990000", 0))) / 1_000_000,
            "write_us"          : clat_ns["mean"] / 1_000,
            "sync_us"           : sync_ns["mean"] / 1_000,
            "sys_cpu"           : job.get("sys_cpu", 0),
            "usr_cpu"           : job.get("usr_cpu", 0),
            "ctx"               : job.get("ctx", 0),
            "phys_write_ios"    : disk.get("write_ios", 0),
            "phys_write_merges" : disk.get("write_merges", 0),
            "phys_read_sectors" : disk.get("read_sectors", 0),
            "disk_util_pct"     : disk.get("util", 0),
            "runtime_s"         : runtime_s,
        }
    return metrics


def load_blktrace_bytes(results_dir):
    phys = {}
    for mode in MODES:
        path = os.path.join(results_dir, f"phys_write_bytes_{mode}.txt")
        try:
            with open(path) as f:
                phys[mode] = int(f.read().strip() or 0)
        except (FileNotFoundError, ValueError):
            phys[mode] = None
    return phys


def load_jbd2_trace(results_dir):
    jbd2 = {}
    for mode in MODES:
        path = os.path.join(results_dir, f"jbd2_trace_{mode}.txt")
        try:
            with open(path) as f:
                content = f.read()
        except FileNotFoundError:
            jbd2[mode] = None
            continue

        m = re.search(
            r'--- JBD2_TRACE_RESULTS_BEGIN ---\n(.*?)--- JBD2_TRACE_RESULTS_END ---',
            content, re.DOTALL
        )
        if not m:
            jbd2[mode] = None
            continue

        parsed = {}
        for line in m.group(1).splitlines():
            line = line.strip()
            if '=' in line:
                key, _, val = line.partition('=')
                try:
                    parsed[key.strip()] = int(val.strip())
                except ValueError:
                    parsed[key.strip()] = 0
        jbd2[mode] = parsed
    return jbd2


# ── Aggregation across N runs ─────────────────────────────────────────────────

def mean(values):
    return sum(values) / len(values) if values else 0.0


def stddev(values):
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def aggregate_metrics(all_metrics):
    """Average fio metrics across N runs. Returns {mode: {key: (mean, stddev)}}."""
    agg = {mode: {} for mode in MODES}
    keys = list(next(iter(all_metrics)).values().__iter__().__class__  # just get keys from first run
                if False else all_metrics[0][MODES[0]].keys())
    for mode in MODES:
        for key in all_metrics[0][mode].keys():
            vals = [run[mode][key] for run in all_metrics]
            agg[mode][key] = (mean(vals), stddev(vals))
    return agg


def aggregate_blktrace(all_phys):
    """Average physical byte counts across N runs. Returns {mode: (mean, stddev)} or None."""
    agg = {}
    for mode in MODES:
        vals = [run[mode] for run in all_phys if run[mode] is not None]
        if not vals:
            agg[mode] = None
        else:
            agg[mode] = (mean(vals), stddev(vals))
    return agg


def aggregate_jbd2(all_jbd2):
    """Average JBD2 trace counters across N runs. Returns {mode: {key: (mean, stddev)}}."""
    agg = {}
    for mode in MODES:
        # Collect all runs that have data for this mode
        run_dicts = [run[mode] for run in all_jbd2 if run[mode] is not None]
        if not run_dicts:
            agg[mode] = None
            continue
        # Union of all keys
        all_keys = set()
        for d in run_dicts:
            all_keys.update(d.keys())
        agg[mode] = {}
        for key in all_keys:
            vals = [d.get(key, 0) for d in run_dicts]
            agg[mode][key] = (mean(vals), stddev(vals))
    return agg


# ── Formatting helpers ────────────────────────────────────────────────────────

def ms(mean_val, sd, fmt=".2f"):
    """Format a mean±stddev pair in milliseconds."""
    return f"{mean_val:{fmt}}ms ±{sd:{fmt}}"


def us_fmt(mean_val, sd):
    return f"{mean_val:.0f} ±{sd:.0f} µs"


def pct_fmt(mean_val, sd):
    return f"{mean_val:.1f} ±{sd:.1f}%"


def plain(mean_val, sd, fmt=".2f"):
    return f"{mean_val:{fmt}} ±{sd:{fmt}}"


def g(d, key):
    """Safe mean getter from aggregated jbd2 dict; returns (0, 0) for missing."""
    if d is None:
        return (0, 0)
    return d.get(key, (0, 0))


# ── Averaged table printers ───────────────────────────────────────────────────

def avg_table1_throughput(agg, N, W):
    print()
    print(f"  AVG TABLE 1: Throughput  (mean ± stddev across {N} runs)")
    print(f"  {'Mode':<12}  {'IOPS':>22}  {'Bandwidth':>24}  {'Runtime (s)':>20}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode]
        iops_m, iops_s  = m["iops"]
        bw_m,   bw_s    = m["bw_mbs"]
        rt_m,   rt_s    = m["runtime_s"]
        print(f"  {mode:<12}  {iops_m:>9.0f} ±{iops_s:>8.0f}  "
              f"{bw_m:>9.2f} ±{bw_s:>7.2f} MB/s  "
              f"{rt_m:>8.2f} ±{rt_s:>6.2f}s")


def avg_table2_commit_latency(agg, N, W):
    print()
    print(f"  AVG TABLE 2: Total I/O Latency  (mean ± stddev across {N} runs)")
    print(f"  {'Mode':<12}  {'Avg':>22}  {'p50':>22}  {'p99':>22}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode]
        avg_m,  avg_s  = m["avg_ms"]
        p50_m,  p50_s  = m["p50_ms"]
        p99_m,  p99_s  = m["p99_ms"]
        print(f"  {mode:<12}  {avg_m:>8.2f} ±{avg_s:>7.2f}ms  "
              f"{p50_m:>8.2f} ±{p50_s:>7.2f}ms  "
              f"{p99_m:>8.2f} ±{p99_s:>7.2f}ms")
    print()
    print(f"  {'Mode':<12}  {'p99.9':>22}  {'p99.99':>22}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode]
        p999_m,  p999_s  = m["p999_ms"]
        p9990_m, p9990_s = m["p9990_ms"]
        print(f"  {mode:<12}  {p999_m:>8.2f} ±{p999_s:>7.2f}ms  "
              f"{p9990_m:>8.2f} ±{p9990_s:>7.2f}ms")


def avg_table3_overhead(agg, N, W):
    nj_iops_m, _  = agg["nojournal"]["iops"]
    nj_avg_m,  _  = agg["nojournal"]["avg_ms"]
    nj_p99_m,  _  = agg["nojournal"]["p99_ms"]
    print()
    print(f"  AVG TABLE 3: Performance Overhead  (relative to nojournal mean, across {N} runs)")
    print(f"  {'Mode':<12}  {'IOPS penalty':>14}  {'Lat overhead':>14}  {'p99 overhead':>14}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        if mode == "nojournal":
            print(f"  {mode:<12}  {'baseline':>14}  {'baseline':>14}  {'baseline':>14}")
        else:
            iops_m, _ = agg[mode]["iops"]
            avg_m,  _ = agg[mode]["avg_ms"]
            p99_m,  _ = agg[mode]["p99_ms"]
            iops_pen = (nj_iops_m - iops_m) / nj_iops_m * 100 if nj_iops_m else 0
            lat_over = (avg_m - nj_avg_m)   / nj_avg_m   * 100 if nj_avg_m  else 0
            p99_over = (p99_m - nj_p99_m)   / nj_p99_m   * 100 if nj_p99_m  else 0
            print(f"  {mode:<12}  {iops_pen:>12.1f}%  {lat_over:>12.1f}%  {p99_over:>12.1f}%")
    print()
    print("  Overhead is computed from the mean of each mode vs the mean of nojournal.")


def avg_table4_iops_variability(agg, N, W):
    print()
    print(f"  AVG TABLE 4: IOPS Variability  (mean ± stddev across {N} runs)")
    print(f"  {'Mode':<12}  {'Min IOPS':>20}  {'Max IOPS':>20}  {'Std Dev':>20}  {'CV (%)':>14}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode]
        min_m, min_s   = m["iops_min"]
        max_m, max_s   = m["iops_max"]
        std_m, std_s   = m["iops_stddev"]
        iops_m, iops_s = m["iops"]
        cv_m = (std_m / iops_m * 100) if iops_m > 0 else 0
        # stddev of CV is approximated from the distribution of std/mean
        cv_s = (std_s / iops_m * 100) if iops_m > 0 else 0
        print(f"  {mode:<12}  {min_m:>8.0f} ±{min_s:>7.0f}  "
              f"{max_m:>8.0f} ±{max_s:>7.0f}  "
              f"{std_m:>8.1f} ±{std_s:>7.1f}  "
              f"{cv_m:>6.1f} ±{cv_s:>5.1f}%")


def avg_table5_latency_split(agg, N, W):
    print()
    print(f"  AVG TABLE 5: Latency Split -- write() vs fdatasync()  (mean ± stddev across {N} runs)")
    print(f"  {'Mode':<12}  {'write() avg':>24}  {'fdatasync() avg':>24}  {'fdatasync%':>12}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode]
        wr_m, wr_s  = m["write_us"]
        sy_m, sy_s  = m["sync_us"]
        total       = wr_m + sy_m
        pct         = (sy_m / total * 100) if total > 0 else 0
        print(f"  {mode:<12}  {wr_m:>10.0f} ±{wr_s:>8.0f} µs  "
              f"{sy_m:>10.0f} ±{sy_s:>8.0f} µs  "
              f"{pct:>10.1f}%")


def avg_table6_cpu_efficiency(agg, N, W):
    print()
    print(f"  AVG TABLE 6: CPU & Kernel Efficiency  (mean ± stddev across {N} runs)")
    print(f"  {'Mode':<12}  {'sys_cpu':>20}  {'usr_cpu':>20}  {'total_cpu':>20}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode]
        sys_m, sys_s = m["sys_cpu"]
        usr_m, usr_s = m["usr_cpu"]
        tot_m = sys_m + usr_m
        tot_s = math.sqrt(sys_s**2 + usr_s**2)  # propagated in quadrature
        print(f"  {mode:<12}  {sys_m:>7.2f} ±{sys_s:>6.2f}%  "
              f"{usr_m:>7.2f} ±{usr_s:>6.2f}%  "
              f"{tot_m:>7.2f} ±{tot_s:>6.2f}%")


def avg_table7_device_io(agg, N, W):
    print()
    print(f"  AVG TABLE 7: Device-Level I/O  (mean ± stddev across {N} runs)")
    print(f"  {'Mode':<12}  {'Disk util%':>20}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        util_m, util_s = agg[mode]["disk_util_pct"]
        print(f"  {mode:<12}  {util_m:>7.1f} ±{util_s:>6.1f}%")


def avg_table8_write_amplification(agg_phys, agg_fio, N, W):
    if all(v is None for v in agg_phys.values()):
        return

    nj = agg_phys.get("nojournal")
    print()
    print(f"  AVG TABLE 8: Write Amplification Factor  (mean ± stddev across {N} runs)")
    print("  WAF = physical bytes (with journal) / physical bytes (no journal).")
    print()

    if nj is not None:
        nj_mb_m = nj[0] / (1024**2)
        nj_mb_s = nj[1] / (1024**2)
        print(f"  No-journal baseline: {nj_mb_m:.4f} ±{nj_mb_s:.4f} MB physical")
        print()
        print(f"  {'Mode':<12}  {'Physical MB':>26}  {'WAF':>14}")
        print("  " + "-" * (W - 2))
        for mode in MODES:
            v = agg_phys[mode]
            if v is None:
                print(f"  {mode:<12}  {'N/A':>26}  {'N/A':>14}")
            else:
                phys_m = v[0] / (1024**2)
                phys_s = v[1] / (1024**2)
                waf_m = phys_m / nj_mb_m if nj_mb_m > 0 else 0
                # Error propagation: WAF = A/B, sigma_WAF/WAF = sqrt((sA/A)^2+(sB/B)^2)
                rel_err = math.sqrt((phys_s/phys_m)**2 + (nj_mb_s/nj_mb_m)**2) if (phys_m > 0 and nj_mb_m > 0) else 0
                waf_s = waf_m * rel_err
                print(f"  {mode:<12}  {phys_m:>12.4f} ±{phys_s:>9.4f} MB  "
                      f"{waf_m:>7.4f} ±{waf_s:>5.4f}x")
    else:
        print("  NOTE: No nojournal baseline. Showing raw physical/logical ratio.")
        print(f"  {'Mode':<12}  {'Logical MB':>14}  {'Physical MB':>26}  {'WAF':>14}")
        print("  " + "-" * (W - 2))
        for mode in MODES:
            log_m, _ = agg_fio[mode]["logical_mb"]
            v = agg_phys[mode]
            if v is None:
                print(f"  {mode:<12}  {log_m:>13.4f}  {'N/A':>26}  {'N/A':>14}")
            else:
                phys_m = v[0] / (1024**2)
                phys_s = v[1] / (1024**2)
                waf_m = phys_m / log_m if log_m > 0 else 0
                waf_s = phys_s / log_m if log_m > 0 else 0
                print(f"  {mode:<12}  {log_m:>13.4f}  "
                      f"{phys_m:>12.4f} ±{phys_s:>9.4f} MB  "
                      f"{waf_m:>7.4f} ±{waf_s:>5.4f}x")


def avg_table9_jbd2(agg_jbd2, agg_fio, N, W):
    if all(v is None for v in agg_jbd2.values()):
        return

    col_w = 26  # wide enough for "mean ±stddev" format

    def fmt_count(d, mode, key):
        if d is None or mode == "nojournal":
            return "(disabled)"
        mean_v, sd_v = g(d, key)
        return f"{mean_v:>8.0f} ±{sd_v:>6.0f}"

    def fmt_ms_derived(mean_v, sd_v):
        return f"{mean_v:>7.3f} ±{sd_v:>5.3f}ms"

    print()
    print("=" * W)
    print(f"  AVG TABLE 9: JBD2 Tracing  (mean ± stddev across {N} runs)")
    print("=" * W)
    print("  All tracepoints filtered to nvme0n1p6 only.")
    print("  nojournal: JBD2 disabled -- no journal activity.")

    # ── 9A: Tracepoint call counts ────────────────────────────────────────────
    print()
    print("  AVG TABLE 9A: Tracepoint Call Counts")
    print(f"  {'Tracepoint':<30}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    rows_9a = [
        ("jbd2_start_commit",      "COMMIT_COUNT"),
        ("jbd2_commit_locking",    "LOCKING_COUNT"),
        ("jbd2_commit_flushing",   "FLUSHING_COUNT"),
        ("jbd2_commit_logging",    "LOGGING_COUNT"),
        ("jbd2_end_commit",        "END_COMMIT_COUNT"),
        ("jbd2_run_stats",         "RUN_STATS_COUNT"),
        ("jbd2_checkpoint",        "CHECKPOINT_COUNT"),
        ("jbd2_checkpoint_stats",  "CHECKPOINT_STATS_COUNT"),
        ("jbd2_handle_start",      "HANDLE_START_COUNT"),
        ("jbd2_handle_stats",      "HANDLE_STATS_COUNT"),
        ("  └ sync handles",       "HANDLE_SYNC_COUNT"),
        ("jbd2_handle_extend",     "HANDLE_EXTEND_COUNT"),
        ("jbd2_handle_restart",    "HANDLE_RESTART_COUNT"),
        ("jbd2_submit_inode_data", "SUBMIT_INODE_DATA_COUNT"),
        ("jbd2_update_log_tail",   "UPDATE_LOG_TAIL_COUNT"),
        ("jbd2_write_superblock",  "WRITE_SUPERBLOCK_COUNT"),
        ("jbd2_lock_buffer_stall", "LOCK_BUFFER_STALL_COUNT"),
    ]
    for label, key in rows_9a:
        row = f"  {label:<30}"
        for mode in MODES:
            d = agg_jbd2[mode]
            row += f"{fmt_count(d, mode, key):>{col_w}}"
        print(row)

    # ── 9B: Average time per commit pipeline stage ────────────────────────────
    print()
    print("  AVG TABLE 9B: Average Time per Commit Pipeline Stage")
    print(f"  {'Stage':<28}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    def run_stats_avg(d, mode, sum_key):
        if d is None or mode == "nojournal":
            return None, None
        n_m, n_s   = g(d, "RUN_STATS_COUNT")
        s_m, s_s   = g(d, sum_key)
        if n_m == 0:
            return 0.0, 0.0
        avg_m = s_m / n_m
        # Propagate uncertainty: avg = S/N, sigma_avg ≈ sigma_S/N (dominant term)
        avg_s = s_s / n_m
        return avg_m, avg_s

    def tp_avg(d, mode, total_us_key, count_key):
        if d is None or mode == "nojournal":
            return None, None
        n_m, n_s = g(d, count_key)
        s_m, s_s = g(d, total_us_key)
        if n_m == 0:
            return 0.0, 0.0
        avg_m = s_m / n_m / 1000.0   # µs → ms
        avg_s = s_s / n_m / 1000.0
        return avg_m, avg_s

    rows_9b = [
        ("wait (handle blocked)",     lambda d, m: run_stats_avg(d, m, "RUN_STATS_SUM_WAIT_MS"),     None),
        ("request_delay",             lambda d, m: run_stats_avg(d, m, "RUN_STATS_SUM_REQDELAY_MS"), None),
        ("running (txn open)",        lambda d, m: run_stats_avg(d, m, "RUN_STATS_SUM_RUNNING_MS"),  None),
        ("locking (txn close)",       lambda d, m: tp_avg(d, m, "LOCKING_TOTAL_US", "LOCKING_COUNT"),
                                      lambda d, m: run_stats_avg(d, m, "RUN_STATS_SUM_LOCKED_MS")),
        ("flushing (data writeback)", lambda d, m: tp_avg(d, m, "FLUSHING_TOTAL_US", "FLUSHING_COUNT"),
                                      lambda d, m: run_stats_avg(d, m, "RUN_STATS_SUM_FLUSHING_MS")),
        ("logging (journal write)",   lambda d, m: tp_avg(d, m, "LOGGING_TOTAL_US", "LOGGING_COUNT"),
                                      lambda d, m: run_stats_avg(d, m, "RUN_STATS_SUM_LOGGING_MS")),
        ("end-to-end commit",         lambda d, m: tp_avg(d, m, "COMMIT_TOTAL_US", "COMMIT_COUNT"),  None),
    ]

    for label, primary_fn, fallback_fn in rows_9b:
        row = f"  {label:<28}"
        for mode in MODES:
            d = agg_jbd2[mode]
            if mode == "nojournal" or d is None:
                row += f"{'(disabled)':>{col_w}}"
                continue
            val_m, val_s = primary_fn(d, mode)
            if val_m is None and fallback_fn is not None:
                val_m, val_s = fallback_fn(d, mode)
            if val_m is None:
                row += f"{'N/A':>{col_w}}"
            else:
                cell = fmt_ms_derived(val_m, val_s)
                row += f"{cell:>{col_w}}"
        print(row)

    # ── 9C: Per-transaction workload ──────────────────────────────────────────
    print()
    print("  AVG TABLE 9C: Per-Transaction Workload & JBD2 Time Budget")
    print(f"  {'Metric':<34}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    def ratio_avg(d, mode, num_key, den_key):
        if d is None or mode == "nojournal":
            return "(disabled)"
        n_m, _ = g(d, den_key)
        s_m, s_s = g(d, num_key)
        if n_m == 0:
            return "0"
        return f"{s_m / n_m:.1f} ±{s_s / n_m:.1f}"

    def pct_jbd2(d, mode, total_us_key, rt_pair):
        if d is None or mode == "nojournal":
            return "(disabled)"
        rt_m, rt_s = rt_pair
        commit_m, commit_s = g(d, total_us_key)
        rt_us_m = rt_m * 1_000_000
        pct_m = (commit_m / rt_us_m * 100) if rt_us_m > 0 else 0
        pct_s = (commit_s / rt_us_m * 100) if rt_us_m > 0 else 0
        return f"{pct_m:.1f} ±{pct_s:.1f}%"

    rows_9c = [
        ("total transactions in journal",
         lambda d, m: "(disabled)" if (d is None or m == "nojournal")
                      else f"{g(d, 'COMMIT_COUNT')[0]:>8.0f} ±{g(d, 'COMMIT_COUNT')[1]:>5.0f}"),
        ("avg handles / txn",
         lambda d, m: ratio_avg(d, m, "RUN_STATS_SUM_HANDLES", "RUN_STATS_COUNT")),
        ("avg blocks dirtied / txn",
         lambda d, m: ratio_avg(d, m, "RUN_STATS_SUM_BLOCKS", "RUN_STATS_COUNT")),
        ("avg blocks_logged / txn",
         lambda d, m: ratio_avg(d, m, "RUN_STATS_SUM_BLOCKS_LOGGED", "RUN_STATS_COUNT")),
        ("avg blocks freed / tail update",
         lambda d, m: ratio_avg(d, m, "LOG_TAIL_SUM_FREED", "UPDATE_LOG_TAIL_COUNT")),
        ("avg blocks written / checkpoint",
         lambda d, m: ratio_avg(d, m, "CHKPT_SUM_WRITTEN", "CHECKPOINT_STATS_COUNT")),
        ("JBD2 commit time / bench time",
         lambda d, m: pct_jbd2(d, m, "COMMIT_TOTAL_US", agg_fio[m]["runtime_s"])),
        ("lock_buffer_stall total (ms)",
         lambda d, m: "(disabled)" if (d is None or m == "nojournal")
                      else f"{g(d, 'LOCK_BUFFER_STALL_TOTAL_MS')[0]:>6.0f} ±{g(d, 'LOCK_BUFFER_STALL_TOTAL_MS')[1]:>5.0f}"),
    ]

    for label, fn in rows_9c:
        row = f"  {label:<34}"
        for mode in MODES:
            val = fn(agg_jbd2[mode], mode)
            row += f"{val:>{col_w}}"
        print(row)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    run_dirs = sys.argv[1:]
    N = len(run_dirs)

    # Validate all directories up front before doing any work.
    for d in run_dirs:
        if not os.path.isdir(d):
            print(f"ERROR: Results directory not found: {d}")
            sys.exit(1)

    print()
    print(f"  Loading results from {N} run(s):")
    for d in run_dirs:
        print(f"    {d}")

    all_fio   = []
    all_phys  = []
    all_jbd2  = []

    for i, d in enumerate(run_dirs):
        try:
            all_fio.append(load_fio_metrics(d))
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
            print(f"ERROR: Failed to load fio results from {d}: {e}")
            sys.exit(1)
        all_phys.append(load_blktrace_bytes(d))
        all_jbd2.append(load_jbd2_trace(d))

    agg_fio  = aggregate_metrics(all_fio)
    agg_phys = aggregate_blktrace(all_phys)
    agg_jbd2 = aggregate_jbd2(all_jbd2)

    nojournal_phys = agg_phys.get("nojournal")

    W = 90

    print()
    print("=" * W)
    print(f"  WAL WORKLOAD AVERAGED RESULTS  ({N} runs · 1 thread · 100 MB · fsync after every write)")
    print("=" * W)

    avg_table1_throughput(agg_fio, N, W)
    avg_table2_commit_latency(agg_fio, N, W)
    avg_table3_overhead(agg_fio, N, W)
    avg_table4_iops_variability(agg_fio, N, W)
    avg_table5_latency_split(agg_fio, N, W)
    avg_table6_cpu_efficiency(agg_fio, N, W)
    avg_table7_device_io(agg_fio, N, W)
    avg_table8_write_amplification(agg_phys, agg_fio, N, W)
    avg_table9_jbd2(agg_jbd2, agg_fio, N, W)

    print()
    print("=" * W)
    print("  INTERPRETATION GUIDE")
    print("=" * W)
    print()
    print("  All values are mean ± sample stddev across the N runs.")
    print("  A small stddev confirms the result is stable and not a one-off artifact.")
    print()
    print("  AVG TABLE 1  IOPS/bandwidth: useful throughput per mode, averaged.")
    print("  AVG TABLE 2  Commit latency: fdatasync() round-trip time, averaged.")
    print("  AVG TABLE 3  Overhead %: computed from the means (not averaged overhead).")
    print("  AVG TABLE 4  IOPS variability: within-run min/max/stddev, then averaged across runs.")
    print("  AVG TABLE 5  Latency split: write() vs fdatasync() share, averaged.")
    print("  AVG TABLE 6  CPU usage averaged across runs.")
    print("  AVG TABLE 7  Disk utilisation averaged across runs.")
    print("  AVG TABLE 8  WAF: physical/nojournal ratio, with propagated uncertainty.")
    print("  AVG TABLE 9  JBD2 tracing: commit counts and stage timings, averaged.")
    print()
    print("  Source run directories:")
    for d in run_dirs:
        print(f"    {d}")
    print()


if __name__ == "__main__":
    main()
