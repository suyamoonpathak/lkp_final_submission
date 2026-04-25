#!/usr/bin/env python3
"""
analyse_averaged_results.py

Accepts N result directories produced by repeated runs of run_combined_analysis.sh
and prints averaged tables (mean ± stddev) using the same structure as
analyse_combined_results.py.

Usage:   python3 analyse_averaged_results.py <dir1> [dir2 ...] [dirN]
Example: python3 analyse_averaged_results.py results/20260422_1000 results/20260422_1100
"""

import json
import math
import os
import re
import sys


MODES     = ["ordered", "journal", "writeback", "nojournal"]
WORKLOADS = ["wal", "seq"]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_fio_metrics(results_dir):
    """Load fio JSON for all modes and both workloads.

    Returns {mode: {wl: metrics_dict}} where metrics_dict matches
    the keys produced by extract_metrics() in analyse_combined_results.py.
    """
    out = {}
    for mode in MODES:
        out[mode] = {}
        for wl in WORKLOADS:
            path = os.path.join(results_dir, f"{wl}_{mode}.json")
            try:
                with open(path) as f:
                    raw = json.load(f)
            except FileNotFoundError:
                print(f"ERROR: Result file not found: {path}")
                sys.exit(1)

            job     = raw["jobs"][0]
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

            runtime_s = job["job_runtime"] / 1000

            out[mode][wl] = {
                "iops"        : write["iops"],
                "bw_mbs"      : write["bw"] / 1024,
                "total_gb"    : write["io_bytes"] / (1024**3),
                "total_ios"   : write["total_ios"],
                "logical_mb"  : write["io_bytes"] / (1024**2),
                "iops_min"    : write["iops_min"],
                "iops_max"    : write["iops_max"],
                "iops_stddev" : write["iops_stddev"],
                "avg_ms"      : (write_lat_ns["mean"] + sync_ns["mean"]) / 1_000_000,
                "p50_ms"      : (float(write_pct.get("50.000000", 0)) + float(sync_pct.get("50.000000", 0))) / 1_000_000,
                "p99_ms"      : (float(write_pct.get("99.000000", 0)) + float(sync_pct.get("99.000000", 0))) / 1_000_000,
                "p999_ms"     : (float(write_pct.get("99.900000", 0)) + float(sync_pct.get("99.900000", 0))) / 1_000_000,
                "p9990_ms"    : (float(write_pct.get("99.990000", 0)) + float(sync_pct.get("99.990000", 0))) / 1_000_000,
                "write_us"    : clat_ns["mean"] / 1_000,
                "sync_us"     : sync_ns["mean"] / 1_000,
                "sys_cpu"     : job.get("sys_cpu", 0),
                "usr_cpu"     : job.get("usr_cpu", 0),
                "ctx"         : job.get("ctx", 0),
                "runtime_s"   : runtime_s,
            }
    return out


def load_blktrace_bytes(results_dir):
    """Returns {mode: int_bytes_or_None}."""
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
    """Returns {mode: dict_of_int_values_or_None}."""
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


# ── Aggregation ───────────────────────────────────────────────────────────────

def _mean(vals):
    return sum(vals) / len(vals)

def _stddev(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def aggregate_fio_metrics(all_metrics):
    """all_metrics: list of {mode: {wl: metrics_dict}} (one per run).

    Returns {mode: {wl: {key: (mean, stddev)}}}.
    """
    agg = {}
    keys = list(all_metrics[0][MODES[0]][WORKLOADS[0]].keys())
    for mode in MODES:
        agg[mode] = {}
        for wl in WORKLOADS:
            agg[mode][wl] = {}
            for key in keys:
                vals = [run[mode][wl][key] for run in all_metrics]
                agg[mode][wl][key] = (_mean(vals), _stddev(vals))
    return agg


def aggregate_blktrace(all_phys):
    """all_phys: list of {mode: int_or_None}.

    Returns {mode: (mean, stddev) or None}.
    """
    agg = {}
    for mode in MODES:
        vals = [run[mode] for run in all_phys if run[mode] is not None]
        if vals:
            agg[mode] = (_mean(vals), _stddev(vals))
        else:
            agg[mode] = None
    return agg


def aggregate_jbd2(all_jbd2):
    """all_jbd2: list of {mode: dict_or_None}.

    Returns {mode: {key: (mean, stddev)} or None}.
    """
    agg = {}
    for mode in MODES:
        dicts = [run[mode] for run in all_jbd2 if run[mode] is not None]
        if not dicts:
            agg[mode] = None
            continue
        all_keys = set()
        for d in dicts:
            all_keys.update(d.keys())
        agg[mode] = {}
        for key in all_keys:
            vals = [d.get(key, 0) for d in dicts]
            agg[mode][key] = (_mean(vals), _stddev(vals))
    return agg


# ── Formatting helpers ────────────────────────────────────────────────────────

def ms(mean_std):
    m, s = mean_std
    return f"{m:.2f}±{s:.2f}ms"

def f0(mean_std):
    m, s = mean_std
    return f"{m:.0f}±{s:.0f}"

def f2(mean_std):
    m, s = mean_std
    return f"{m:.2f}±{s:.2f}"

def f4(mean_std):
    m, s = mean_std
    return f"{m:.4f}±{s:.4f}"

def g(d, key):
    if d is None:
        return (0, 0)
    return d.get(key, (0, 0))


def section_header(title, W):
    print()
    print("=" * W)
    print(f"  {title}")
    print("=" * W)


# ── Per-workload averaged table printers ──────────────────────────────────────

def avg_table_throughput(agg, wl, W):
    label = "WAL (fsync per write)" if wl == "wal" else "Sequential Write (buffered)"
    print()
    print(f"  TABLE T1 [{wl.upper()}]: Throughput  --  {label}  (mean ± stddev)")
    print(f"  {'Mode':<12}  {'IOPS':>20}  {'Bandwidth':>22}  {'Data Written':>20}  {'Runtime':>16}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode][wl]
        print(f"  {mode:<12}  {f0(m['iops']):>20}  {f2(m['bw_mbs']):>18} MB/s"
              f"  {f2(m['logical_mb']):>17} MB  {f2(m['runtime_s']):>13}s")


def avg_table_latency(agg, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T2 [{wl.upper()}]: I/O Latency  --  {label}  (mean ± stddev)")
    print(f"  {'Mode':<12}  {'Avg':>18}  {'p50':>18}  {'p99':>18}  {'p99.9':>18}  {'p99.99':>18}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = agg[mode][wl]
        print(f"  {mode:<12}  {ms(m['avg_ms']):>18}  {ms(m['p50_ms']):>18}"
              f"  {ms(m['p99_ms']):>18}  {ms(m['p999_ms']):>18}  {ms(m['p9990_ms']):>18}")


def avg_table_overhead(agg, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T3 [{wl.upper()}]: Performance Overhead vs nojournal  --  {label}  (mean values)")
    print(f"  {'Mode':<12}  {'IOPS penalty':>14}  {'Lat overhead':>14}  {'p99 overhead':>14}")
    print("  " + "-" * (W - 2))
    nj = agg["nojournal"][wl]
    for mode in MODES:
        m = agg[mode][wl]
        if mode == "nojournal":
            print(f"  {mode:<12}  {'baseline':>14}  {'baseline':>14}  {'baseline':>14}")
        else:
            nj_iops = nj["iops"][0]
            nj_lat  = nj["avg_ms"][0]
            nj_p99  = nj["p99_ms"][0]
            iops_pen = (nj_iops - m["iops"][0]) / nj_iops * 100 if nj_iops else 0
            lat_over = (m["avg_ms"][0] - nj_lat)  / nj_lat  * 100 if nj_lat  else 0
            p99_over = (m["p99_ms"][0] - nj_p99)  / nj_p99  * 100 if nj_p99  else 0
            print(f"  {mode:<12}  {iops_pen:>12.1f}%  {lat_over:>12.1f}%  {p99_over:>12.1f}%")


def avg_table_iops_variability(agg, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T4 [{wl.upper()}]: IOPS Variability  --  {label}  (mean ± stddev)")
    print(f"  {'Mode':<12}  {'Min':>16}  {'Max':>16}  {'Std Dev':>16}  {'CV (%)':>14}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m  = agg[mode][wl]
        mean_iops = m["iops"][0]
        mean_cv   = (m["iops_stddev"][0] / mean_iops * 100) if mean_iops > 0 else 0
        std_cv    = (m["iops_stddev"][1] / mean_iops * 100) if mean_iops > 0 else 0
        print(f"  {mode:<12}  {f0(m['iops_min']):>16}  {f0(m['iops_max']):>16}"
              f"  {f2(m['iops_stddev']):>16}  {mean_cv:>7.1f}±{std_cv:.1f}%")


def avg_table_latency_split(agg, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T5 [{wl.upper()}]: Latency Breakdown -- write() vs fsync()  --  {label}  (mean ± stddev)")
    print(f"  {'Mode':<12}  {'write() avg':>22}  {'fsync() avg':>22}  {'fsync share':>14}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m         = agg[mode][wl]
        wus_m, wus_s = m["write_us"]
        sus_m, sus_s = m["sync_us"]
        total_us  = wus_m + sus_m
        fsync_pct = (sus_m / total_us * 100) if total_us > 0 else 0
        print(f"  {mode:<12}  {f2(m['write_us']):>18} µs  {f2(m['sync_us']):>18} µs  {fsync_pct:>12.1f}%")


def avg_table_cpu(agg, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T6 [{wl.upper()}]: CPU & Kernel Efficiency  --  {label}  (mean ± stddev)")
    print(f"  {'Mode':<12}  {'sys_cpu':>16}  {'usr_cpu':>16}  {'total_cpu':>16}  {'ctx sw / IO':>14}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m             = agg[mode][wl]
        sys_m, sys_s  = m["sys_cpu"]
        usr_m, usr_s  = m["usr_cpu"]
        tot_m = sys_m + usr_m
        tot_s = math.sqrt(sys_s**2 + usr_s**2)
        ctx_m, ctx_s  = m["ctx"]
        tios_m, _     = m["total_ios"]
        cpio_m = ctx_m / tios_m if tios_m > 0 else 0
        print(f"  {mode:<12}  {f2((sys_m, sys_s)):>14}%  {f2((usr_m, usr_s)):>14}%"
              f"  {f2((tot_m, tot_s)):>14}%  {cpio_m:>13.2f}")


# ── Combined-trace averaged table printers ───────────────────────────────────

def avg_table_write_amplification(agg_fio, agg_phys, W):
    if all(v is None for v in agg_phys.values()):
        return

    def combined_logical_mb_mean(mode):
        return agg_fio[mode]["wal"]["logical_mb"][0] + agg_fio[mode]["seq"]["logical_mb"][0]

    nojournal_phys = agg_phys.get("nojournal")

    print()
    print("  TABLE T7: Combined Write Amplification Factor (blktrace on nvme0n1p6)  (mean ± stddev)")
    print("  Physical bytes cover both WAL and seq jobs running concurrently.")
    print("  WAF = physical bytes (with journal) / physical bytes (nojournal baseline).")
    print()

    if nojournal_phys is not None:
        nj_m, nj_s = nojournal_phys
        nj_mb_m    = nj_m / (1024**2)
        nj_mb_s    = nj_s / (1024**2)
        print(f"  No-journal baseline: {nj_mb_m:.4f}±{nj_mb_s:.4f} MB physical")
        print()
        print(f"  {'Mode':<12}  {'Physical MB (mean±std)':>26}  {'Journaling WAF':>16}")
        print("  " + "-" * (W - 2))
        for mode in MODES:
            p = agg_phys[mode]
            if p is None:
                print(f"  {mode:<12}  {'N/A':>26}  {'N/A':>16}")
            else:
                pm, ps = p
                phys_mb_m = pm / (1024**2)
                phys_mb_s = ps / (1024**2)
                waf_m = phys_mb_m / nj_mb_m if nj_mb_m > 0 else 0
                print(f"  {mode:<12}  {f4((phys_mb_m, phys_mb_s)):>26}  {waf_m:>15.4f}x")
    else:
        print("  NOTE: No nojournal baseline found. Showing raw physical/logical ratio.")
        print(f"  {'Mode':<12}  {'Logical MB':>14}  {'Physical MB (mean±std)':>26}  {'WAF':>10}")
        print("  " + "-" * (W - 2))
        for mode in MODES:
            logical_mb = combined_logical_mb_mean(mode)
            p = agg_phys[mode]
            if p is None:
                print(f"  {mode:<12}  {logical_mb:>13.4f}  {'N/A':>26}  {'N/A':>10}")
            else:
                pm, ps = p
                phys_mb_m = pm / (1024**2)
                phys_mb_s = ps / (1024**2)
                waf_m = phys_mb_m / logical_mb if logical_mb > 0 else 0
                print(f"  {mode:<12}  {logical_mb:>13.4f}  {f4((phys_mb_m, phys_mb_s)):>26}  {waf_m:>9.4f}x")


def avg_table_jbd2_activity(agg_jbd2, agg_fio, W):
    if all(v is None for v in agg_jbd2.values()):
        return

    col_w = 20

    def gm(d, key):
        if d is None:
            return (0, 0)
        return d.get(key, (0, 0))

    def combined_runtime_s_mean(mode):
        return max(agg_fio[mode]["wal"]["runtime_s"][0], agg_fio[mode]["seq"]["runtime_s"][0])

    def ms_fmt(mean_std):
        m, s = mean_std
        return f"{m:.3f}±{s:.3f}ms"

    print()
    print("  TABLE T8: Combined JBD2 Journal Tracing (bpftrace)  (mean ± stddev)")
    print("  Covers both WAL and seq jobs running concurrently.")
    print("  All tracepoints filtered to nvme0n1p6 only.")
    print("  nojournal: JBD2 disabled -- no journal activity.")

    # ── T8A: Tracepoint call counts ───────────────────────────────────────────
    print()
    print("  TABLE T8A: Tracepoint Call Counts  (both jobs combined)  (mean ± stddev)")
    print(f"  {'Tracepoint':<30}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    rows_a = [
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
    for label_row, key in rows_a:
        row = f"  {label_row:<30}"
        for mode in MODES:
            d = agg_jbd2[mode]
            if d is None:
                row += f"{'N/A':>{col_w}}"
            elif mode == "nojournal":
                row += f"{'(disabled)':>{col_w}}"
            else:
                m_val, s_val = gm(d, key)
                row += f"{f0((m_val, s_val)):>{col_w}}"
        print(row)

    # ── T8B: Average commit pipeline stage timing ─────────────────────────────
    print()
    print("  TABLE T8B: Average Time per Commit Pipeline Stage  (both jobs combined)  (mean ± stddev)")
    print(f"  {'Stage':<28}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    def run_stats_avg(d, mode, sum_key):
        if d is None or mode == "nojournal":
            return None
        n_m, n_s = gm(d, "RUN_STATS_COUNT")
        s_m, s_s = gm(d, sum_key)
        if n_m == 0:
            return (0.0, 0.0)
        mean = s_m / n_m
        std  = abs(s_s / n_m) if n_m > 0 else 0
        return (mean, std)

    def tp_avg(d, mode, total_us_key, count_key):
        if d is None or mode == "nojournal":
            return None
        n_m, n_s  = gm(d, count_key)
        us_m, us_s = gm(d, total_us_key)
        if n_m == 0:
            return (0.0, 0.0)
        mean = us_m / n_m / 1000.0
        std  = us_s / n_m / 1000.0
        return (mean, std)

    rows_b = [
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
    for label_row, primary_fn, fallback_fn in rows_b:
        row = f"  {label_row:<28}"
        for mode in MODES:
            d = agg_jbd2[mode]
            if mode == "nojournal" or d is None:
                row += f"{'(disabled)':>{col_w}}"
                continue
            val = primary_fn(d, mode)
            if val is None and fallback_fn is not None:
                val = fallback_fn(d, mode)
            if val is None:
                row += f"{'N/A':>{col_w}}"
            else:
                row += f"{ms_fmt(val):>{col_w}}"
        print(row)

    # ── T8C: Per-transaction workload and JBD2 time budget ────────────────────
    print()
    print("  TABLE T8C: Per-Transaction Workload & JBD2 Time Budget  (both jobs combined)  (mean ± stddev)")
    print(f"  {'Metric':<34}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    def fmt_ratio(d, mode, num_key, den_key):
        if d is None or mode == "nojournal":
            return "(disabled)"
        n_m, n_s  = gm(d, den_key)
        nm_m, nm_s = gm(d, num_key)
        if n_m == 0:
            return "0"
        return f"{nm_m / n_m:.1f}±{nm_s / n_m:.1f}"

    def fmt_pct(d, mode, total_us_key):
        if d is None or mode == "nojournal":
            return "(disabled)"
        runtime_us = combined_runtime_s_mean(mode) * 1_000_000
        c_m, c_s   = gm(d, total_us_key)
        pct_m = (c_m / runtime_us * 100) if runtime_us > 0 else 0
        pct_s = (c_s / runtime_us * 100) if runtime_us > 0 else 0
        return f"{pct_m:.1f}±{pct_s:.1f}%"

    rows_c = [
        ("total transactions in journal",
         lambda d, m: "(disabled)" if (d is None or m == "nojournal")
                      else f"{gm(d, 'COMMIT_COUNT')[0]:,.0f}±{gm(d, 'COMMIT_COUNT')[1]:,.0f}"),
        ("avg handles / txn",
         lambda d, m: fmt_ratio(d, m, "RUN_STATS_SUM_HANDLES", "RUN_STATS_COUNT")),
        ("avg blocks dirtied / txn",
         lambda d, m: fmt_ratio(d, m, "RUN_STATS_SUM_BLOCKS", "RUN_STATS_COUNT")),
        ("avg blocks_logged / txn",
         lambda d, m: fmt_ratio(d, m, "RUN_STATS_SUM_BLOCKS_LOGGED", "RUN_STATS_COUNT")),
        ("avg blocks freed / tail update",
         lambda d, m: fmt_ratio(d, m, "LOG_TAIL_SUM_FREED", "UPDATE_LOG_TAIL_COUNT")),
        ("avg blocks written / checkpoint",
         lambda d, m: fmt_ratio(d, m, "CHKPT_SUM_WRITTEN", "CHECKPOINT_STATS_COUNT")),
        ("JBD2 commit time / bench time",
         lambda d, m: fmt_pct(d, m, "COMMIT_TOTAL_US")),
        ("lock_buffer_stall total (ms)",
         lambda d, m: "(disabled)" if (d is None or m == "nojournal")
                      else f"{gm(d, 'LOCK_BUFFER_STALL_TOTAL_MS')[0]:.0f}±{gm(d, 'LOCK_BUFFER_STALL_TOTAL_MS')[1]:.0f}"),
    ]
    for label_row, fn in rows_c:
        row = f"  {label_row:<34}"
        for mode in MODES:
            val = fn(agg_jbd2[mode], mode)
            row += f"{val:>{col_w}}"
        print(row)


# ── Cross-workload averaged comparison ───────────────────────────────────────

def avg_table_cross_workload(agg, W):
    print()
    print("  TABLE X1: Cross-Workload Throughput Comparison  (concurrent run)  (mean ± stddev)")
    print(f"  {'Mode':<12}  {'WAL IOPS':>22}  {'WAL BW':>22}  {'Seq IOPS':>22}  {'Seq BW':>24}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        wm = agg[mode]["wal"]
        sm = agg[mode]["seq"]
        print(f"  {mode:<12}  {f0(wm['iops']):>22}  {f2(wm['bw_mbs']):>18} MB/s"
              f"  {f0(sm['iops']):>22}  {f2(sm['bw_mbs']):>20} MB/s")
    print()
    print("  Both jobs ran concurrently and contended for the same journal.")
    print("  WAL IOPS is depressed compared to isolation due to seq write pressure.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    dirs = sys.argv[1:]
    for d in dirs:
        if not os.path.isdir(d):
            print(f"ERROR: Results directory not found: {d}")
            sys.exit(1)

    N = len(dirs)

    all_metrics = [load_fio_metrics(d)     for d in dirs]
    all_phys    = [load_blktrace_bytes(d)  for d in dirs]
    all_jbd2    = [load_jbd2_trace(d)      for d in dirs]

    agg_fio  = aggregate_fio_metrics(all_metrics)
    agg_phys = aggregate_blktrace(all_phys)
    agg_jbd2 = aggregate_jbd2(all_jbd2)

    W = 110

    print()
    print("=" * W)
    print(f"  COMBINED WORKLOAD AVERAGED RESULTS  (N={N} runs, mean ± stddev)")
    print("  WAL: 4×1GB · 8KB · fsync/write   +   Seq: 4×10GB · 1MB · buffered")
    print("=" * W)

    section_header("WAL WORKLOAD  (wal-simulation · fsync after every 8KB write)", W)
    avg_table_throughput(agg_fio, "wal", W)
    avg_table_latency(agg_fio, "wal", W)
    avg_table_overhead(agg_fio, "wal", W)
    avg_table_iops_variability(agg_fio, "wal", W)
    avg_table_latency_split(agg_fio, "wal", W)
    avg_table_cpu(agg_fio, "wal", W)

    section_header("SEQUENTIAL WRITE WORKLOAD  (seq-write · 1MB blocks · buffered)", W)
    avg_table_throughput(agg_fio, "seq", W)
    avg_table_latency(agg_fio, "seq", W)
    avg_table_overhead(agg_fio, "seq", W)
    avg_table_iops_variability(agg_fio, "seq", W)
    avg_table_latency_split(agg_fio, "seq", W)
    avg_table_cpu(agg_fio, "seq", W)

    section_header("COMBINED TRACES  (blktrace + JBD2 covering both concurrent jobs)", W)
    avg_table_write_amplification(agg_fio, agg_phys, W)
    avg_table_jbd2_activity(agg_jbd2, agg_fio, W)

    section_header("CROSS-WORKLOAD COMPARISON", W)
    avg_table_cross_workload(agg_fio, W)

    print()
    print("=" * W)
    print("  INTERPRETATION GUIDE")
    print("=" * W)
    print()
    print("  T1  Throughput: IOPS and bandwidth per mode, per job.")
    print("  T2  Latency: per-I/O round-trip including any fsync() cost.")
    print("  T3  Overhead %: performance penalty of journaling vs nojournal baseline (mean values).")
    print("  T4  IOPS variability: high CV% means journal checkpoint stall spikes.")
    print("  T5  Latency split: fraction of time in fsync() vs write().")
    print("  T6  CPU: kernel (JBD2 commit, writeback) vs user time per job.")
    print("  T7  WAF: combined physical bytes / nojournal -- total journaling tax.")
    print("  T8  JBD2 tracing: commit counts, stage timing, % bench time in journal.")
    print("  X1  Side-by-side throughput showing concurrent contention effects.")
    print()
    print(f"  Averaged across {N} run(s):")
    for d in dirs:
        print(f"    {d}")
    print()


if __name__ == "__main__":
    main()
