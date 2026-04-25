#!/usr/bin/env python3
"""
analyse_combined_results.py

Parses fio JSON output from the combined journaling benchmark and produces
a comprehensive comparison of ordered / journal / writeback / nojournal modes
for two workloads run concurrently as separate fio processes:
  - wal_workload.fio: sync-heavy, 8KB sequential writes with fsync() after every write
  - seq_write.fio:    bulk 1MB sequential writes, buffered, no per-write sync

Each fio process has group_reporting=1 in its own [global], so each produces
exactly one aggregated result block in its own JSON file regardless of numjobs.
blktrace and bpftrace JBD2 tracing captured the combined I/O of both processes.

Called automatically by run_combined_analysis.sh after benchmarking, or run
manually to re-analyse previously saved results without re-benchmarking:

  Usage:   python3 analyse_combined_results.py <results_dir>
  Example: python3 analyse_combined_results.py results/20260422_164708
"""

import json
import os
import re
import sys


MODES    = ["ordered", "journal", "writeback", "nojournal"]
WORKLOADS = ["wal", "seq"]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_results(results_dir):
    """Load fio JSON for all modes. Returns {mode: {wl_key: job_dict}}.

    Each workload produces its own JSON file (wal_<mode>.json / seq_<mode>.json)
    with group_reporting=1, so jobs[0] is always the single aggregated block.
    """
    data = {}
    for mode in MODES:
        data[mode] = {}
        for wl in WORKLOADS:
            path = os.path.join(results_dir, f"{wl}_{mode}.json")
            try:
                with open(path) as f:
                    raw = json.load(f)
            except FileNotFoundError:
                print(f"ERROR: Result file not found: {path}")
                sys.exit(1)
            data[mode][wl] = raw["jobs"][0]

    return data


def extract_metrics(data):
    """Pull every field we need from the fio job dicts into a flat dict."""
    metrics = {}
    for mode in MODES:
        metrics[mode] = {}
        for wl_key in WORKLOADS:
            job     = data[mode][wl_key]
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

            metrics[mode][wl_key] = {
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
    return metrics


def load_blktrace_bytes(results_dir):
    """Load combined physical write bytes (both jobs ran in one fio invocation).

    Returns {mode: int_bytes}.
    """
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
    """Load combined JBD2 trace (both jobs ran in one fio invocation).

    Returns {mode: dict_of_int_values}.
    """
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def section_header(title, W):
    print()
    print("=" * W)
    print(f"  {title}")
    print("=" * W)


# ── Per-workload table printers ───────────────────────────────────────────────

def table_throughput(metrics, wl, W):
    label = "WAL (fsync per write)" if wl == "wal" else "Sequential Write (buffered)"
    print()
    print(f"  TABLE T1 [{wl.upper()}]: Throughput  --  {label}")
    print(f"  {'Mode':<12}  {'IOPS':>8}  {'Bandwidth':>12}  {'Data Written':>14}  {'Runtime':>10}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = metrics[mode][wl]
        print(f"  {mode:<12}  {m['iops']:>8.0f}  {m['bw_mbs']:>9.2f} MB/s"
              f"  {m['logical_mb']:>11.2f} MB  {m['runtime_s']:>8.2f}s")


def table_latency(metrics, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T2 [{wl.upper()}]: I/O Latency  --  {label}")
    print(f"  {'Mode':<12}  {'Avg':>10}  {'p50':>10}  {'p99':>10}  {'p99.9':>10}  {'p99.99':>10}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m = metrics[mode][wl]
        print(f"  {mode:<12}  {m['avg_ms']:>8.2f}ms  {m['p50_ms']:>8.2f}ms"
              f"  {m['p99_ms']:>8.2f}ms  {m['p999_ms']:>8.2f}ms  {m['p9990_ms']:>8.2f}ms")


def table_overhead(metrics, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T3 [{wl.upper()}]: Performance Overhead vs nojournal  --  {label}")
    print(f"  {'Mode':<12}  {'IOPS penalty':>14}  {'Lat overhead':>14}  {'p99 overhead':>14}")
    print("  " + "-" * (W - 2))
    nj = metrics["nojournal"][wl]
    for mode in MODES:
        m = metrics[mode][wl]
        if mode == "nojournal":
            print(f"  {mode:<12}  {'baseline':>14}  {'baseline':>14}  {'baseline':>14}")
        else:
            iops_pen = (nj["iops"] - m["iops"]) / nj["iops"] * 100 if nj["iops"] else 0
            lat_over = (m["avg_ms"] - nj["avg_ms"]) / nj["avg_ms"] * 100 if nj["avg_ms"] else 0
            p99_over = (m["p99_ms"] - nj["p99_ms"]) / nj["p99_ms"] * 100 if nj["p99_ms"] else 0
            print(f"  {mode:<12}  {iops_pen:>12.1f}%  {lat_over:>12.1f}%  {p99_over:>12.1f}%")


def table_iops_variability(metrics, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T4 [{wl.upper()}]: IOPS Variability  --  {label}")
    print(f"  {'Mode':<12}  {'Min':>8}  {'Max':>8}  {'Std Dev':>9}  {'CV (%)':>8}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m  = metrics[mode][wl]
        cv = (m["iops_stddev"] / m["iops"] * 100) if m["iops"] > 0 else 0
        print(f"  {mode:<12}  {m['iops_min']:>8.0f}  {m['iops_max']:>8.0f}"
              f"  {m['iops_stddev']:>9.1f}  {cv:>7.1f}%")


def table_latency_split(metrics, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T5 [{wl.upper()}]: Latency Breakdown -- write() vs fsync()  --  {label}")
    print(f"  {'Mode':<12}  {'write() avg':>13}  {'fsync() avg':>14}  {'fsync share':>13}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m         = metrics[mode][wl]
        total_us  = m["write_us"] + m["sync_us"]
        fsync_pct = (m["sync_us"] / total_us * 100) if total_us > 0 else 0
        print(f"  {mode:<12}  {m['write_us']:>10.0f} µs  {m['sync_us']:>11.0f} µs  {fsync_pct:>11.1f}%")


def table_cpu(metrics, wl, W):
    label = "WAL" if wl == "wal" else "Sequential Write"
    print()
    print(f"  TABLE T6 [{wl.upper()}]: CPU & Kernel Efficiency  --  {label}")
    print(f"  {'Mode':<12}  {'sys_cpu':>8}  {'usr_cpu':>8}  {'total_cpu':>10}  {'ctx sw / IO':>12}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        m          = metrics[mode][wl]
        total_cpu  = m["sys_cpu"] + m["usr_cpu"]
        ctx_per_io = m["ctx"] / m["total_ios"] if m["total_ios"] > 0 else 0
        print(f"  {mode:<12}  {m['sys_cpu']:>7.2f}%  {m['usr_cpu']:>7.2f}%"
              f"  {total_cpu:>9.2f}%  {ctx_per_io:>11.2f}")


# ── Combined-trace table printers (one set covers both jobs) ─────────────────

def table_write_amplification(metrics, phys_bytes, W):
    if all(v is None for v in phys_bytes.values()):
        return

    nojournal_phys = phys_bytes.get("nojournal")

    def combined_logical_mb(mode):
        return metrics[mode]["wal"]["logical_mb"] + metrics[mode]["seq"]["logical_mb"]

    print()
    print("  TABLE T7: Combined Write Amplification Factor (blktrace on nvme0n1p6)")
    print("  Physical bytes cover both WAL and seq jobs running concurrently.")
    print("  WAF = physical bytes (with journal) / physical bytes (nojournal baseline).")
    print()

    if nojournal_phys is not None:
        nojournal_mb = nojournal_phys / (1024 ** 2)
        print(f"  No-journal baseline: {nojournal_mb:.4f} MB physical")
        print()
        print(f"  {'Mode':<12}  {'Physical MB':>15}  {'Journaling WAF':>15}")
        print("  " + "-" * (W - 2))
        for mode in MODES:
            phys = phys_bytes[mode]
            if phys is None:
                print(f"  {mode:<12}  {'N/A':>15}  {'N/A':>15}")
            else:
                phys_mb = phys / (1024 ** 2)
                waf     = phys_mb / nojournal_mb if nojournal_mb > 0 else 0
                print(f"  {mode:<12}  {phys_mb:>14.4f}  {waf:>14.4f}x")
    else:
        print("  NOTE: No nojournal baseline found. Showing raw physical/logical ratio.")
        print(f"  {'Mode':<12}  {'Logical MB':>14}  {'Physical MB':>15}  {'WAF':>10}")
        print("  " + "-" * (W - 2))
        for mode in MODES:
            logical_mb = combined_logical_mb(mode)
            phys       = phys_bytes[mode]
            if phys is None:
                print(f"  {mode:<12}  {logical_mb:>13.4f}  {'N/A':>15}  {'N/A':>10}")
            else:
                phys_mb = phys / (1024 ** 2)
                waf     = phys_mb / logical_mb if logical_mb > 0 else 0
                print(f"  {mode:<12}  {logical_mb:>13.4f}  {phys_mb:>14.4f}  {waf:>9.4f}x")


def table_jbd2_activity(jbd2, metrics, W):
    if all(v is None for v in jbd2.values()):
        return

    col_w = 13

    def g(d, key):
        if d is None:
            return 0
        return d.get(key, 0)

    # Use the longer of the two job runtimes as the JBD2 time-budget denominator.
    def combined_runtime_s(mode):
        return max(metrics[mode]["wal"]["runtime_s"], metrics[mode]["seq"]["runtime_s"])

    print()
    print("  TABLE T8: Combined JBD2 Journal Tracing (bpftrace)")
    print("  Covers both WAL and seq jobs running concurrently.")
    print("  All tracepoints filtered to nvme0n1p6 only.")
    print("  nojournal: JBD2 disabled -- no journal activity.")

    # ── 8A: Tracepoint call counts ────────────────────────────────────────────
    print()
    print("  TABLE T8A: Tracepoint Call Counts  (both jobs combined)")
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
            d = jbd2[mode]
            if d is None:
                row += f"{'N/A':>{col_w}}"
            elif mode == "nojournal":
                row += f"{'(disabled)':>{col_w}}"
            else:
                row += f"{g(d, key):>{col_w},}"
        print(row)

    # ── 8B: Average commit pipeline stage timing ───────────────────────────────
    print()
    print("  TABLE T8B: Average Time per Commit Pipeline Stage  (both jobs combined)")
    print(f"  {'Stage':<28}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    def run_stats_avg_ms(d, mode, sum_key):
        if d is None or mode == "nojournal":
            return None
        n = g(d, "RUN_STATS_COUNT")
        return g(d, sum_key) / n if n else 0.0

    def tp_avg_ms(d, mode, total_us_key, count_key):
        if d is None or mode == "nojournal":
            return None
        n = g(d, count_key)
        return g(d, total_us_key) / n / 1000.0 if n else 0.0

    rows_b = [
        ("wait (handle blocked)",     lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_WAIT_MS"),     None),
        ("request_delay",             lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_REQDELAY_MS"), None),
        ("running (txn open)",        lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_RUNNING_MS"),  None),
        ("locking (txn close)",       lambda d, m: tp_avg_ms(d, m, "LOCKING_TOTAL_US", "LOCKING_COUNT"),
                                      lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_LOCKED_MS")),
        ("flushing (data writeback)", lambda d, m: tp_avg_ms(d, m, "FLUSHING_TOTAL_US", "FLUSHING_COUNT"),
                                      lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_FLUSHING_MS")),
        ("logging (journal write)",   lambda d, m: tp_avg_ms(d, m, "LOGGING_TOTAL_US", "LOGGING_COUNT"),
                                      lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_LOGGING_MS")),
        ("end-to-end commit",         lambda d, m: tp_avg_ms(d, m, "COMMIT_TOTAL_US", "COMMIT_COUNT"),  None),
    ]
    for label_row, primary_fn, fallback_fn in rows_b:
        row = f"  {label_row:<28}"
        for mode in MODES:
            d = jbd2[mode]
            if mode == "nojournal" or d is None:
                row += f"{'(disabled)':>{col_w}}"
                continue
            val = primary_fn(d, mode)
            if val is None and fallback_fn is not None:
                val = fallback_fn(d, mode)
            if val is None:
                row += f"{'N/A':>{col_w}}"
            else:
                row += f"{val:>{col_w - 2}.3f}ms"
        print(row)

    # ── 8C: Per-transaction workload and JBD2 time budget ─────────────────────
    print()
    print("  TABLE T8C: Per-Transaction Workload & JBD2 Time Budget  (both jobs combined)")
    print(f"  {'Metric':<34}" + "".join(f"{m:>{col_w}}" for m in MODES))
    print("  " + "-" * (W - 2))

    def fmt_ratio(d, mode, num_key, den_key):
        if d is None or mode == "nojournal":
            return "(disabled)"
        n = g(d, den_key)
        return f"{g(d, num_key) / n:.1f}" if n else "0"

    def fmt_pct(d, mode, total_us_key):
        if d is None or mode == "nojournal":
            return "(disabled)"
        runtime_us = combined_runtime_s(mode) * 1_000_000
        commit_us  = g(d, total_us_key)
        pct = (commit_us / runtime_us * 100) if runtime_us > 0 else 0
        return f"{pct:.1f}%"

    rows_c = [
        ("total transactions in journal",
         lambda d, m: "(disabled)" if (d is None or m == "nojournal")
                      else f"{g(d, 'COMMIT_COUNT'):,}"),
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
                      else str(g(d, "LOCK_BUFFER_STALL_TOTAL_MS"))),
    ]
    for label_row, fn in rows_c:
        row = f"  {label_row:<34}"
        for mode in MODES:
            val = fn(jbd2[mode], mode)
            row += f"{val:>{col_w}}"
        print(row)


# ── Cross-workload comparison ─────────────────────────────────────────────────

def table_cross_workload_comparison(metrics, W):
    print()
    print("  TABLE X1: Cross-Workload Throughput Comparison  (concurrent run)")
    print(f"  {'Mode':<12}  {'WAL IOPS':>10}  {'WAL BW':>10}  {'Seq IOPS':>10}  {'Seq BW':>14}")
    print("  " + "-" * (W - 2))
    for mode in MODES:
        wm = metrics[mode]["wal"]
        sm = metrics[mode]["seq"]
        print(f"  {mode:<12}  {wm['iops']:>10.0f}  {wm['bw_mbs']:>7.2f} MB/s"
              f"  {sm['iops']:>10.0f}  {sm['bw_mbs']:>10.2f} MB/s")
    print()
    print("  Both jobs ran concurrently and contended for the same journal.")
    print("  WAL IOPS is depressed compared to isolation due to seq write pressure.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    results_dir = sys.argv[1]
    if not os.path.isdir(results_dir):
        print(f"ERROR: Results directory not found: {results_dir}")
        sys.exit(1)

    data       = load_results(results_dir)
    metrics    = extract_metrics(data)
    phys_bytes = load_blktrace_bytes(results_dir)
    jbd2       = load_jbd2_trace(results_dir)

    W = 82

    print()
    print("=" * W)
    print("  COMBINED WORKLOAD RESULTS  (both jobs ran concurrently, single fio run)")
    print("  WAL: 4×100MB · 8KB · fsync/write   +   Seq: 4×4GB · 1MB · buffered")
    print("=" * W)

    section_header("WAL WORKLOAD  (wal-simulation · fsync after every 8KB write)", W)
    table_throughput(metrics, "wal", W)
    table_latency(metrics, "wal", W)
    table_overhead(metrics, "wal", W)
    table_iops_variability(metrics, "wal", W)
    table_latency_split(metrics, "wal", W)
    table_cpu(metrics, "wal", W)

    section_header("SEQUENTIAL WRITE WORKLOAD  (seq-write · 1MB blocks · buffered)", W)
    table_throughput(metrics, "seq", W)
    table_latency(metrics, "seq", W)
    table_overhead(metrics, "seq", W)
    table_iops_variability(metrics, "seq", W)
    table_latency_split(metrics, "seq", W)
    table_cpu(metrics, "seq", W)

    section_header("COMBINED TRACES  (blktrace + JBD2 covering both concurrent jobs)", W)
    table_write_amplification(metrics, phys_bytes, W)
    table_jbd2_activity(jbd2, metrics, W)

    section_header("CROSS-WORKLOAD COMPARISON", W)
    table_cross_workload_comparison(metrics, W)

    print()
    print("=" * W)
    print("  INTERPRETATION GUIDE")
    print("=" * W)
    print()
    print("  T1  Throughput: IOPS and bandwidth per mode, per job.")
    print("  T2  Latency: per-I/O round-trip including any fsync() cost.")
    print("  T3  Overhead %: performance penalty of journaling vs nojournal baseline.")
    print("  T4  IOPS variability: high CV% means journal checkpoint stall spikes.")
    print("  T5  Latency split: fraction of time in fsync() vs write().")
    print("  T6  CPU: kernel (JBD2 commit, writeback) vs user time per job.")
    print("  T7  WAF: combined physical bytes / nojournal -- total journaling tax.")
    print("  T8  JBD2 tracing: commit counts, stage timing, % bench time in journal.")
    print("  X1  Side-by-side throughput showing concurrent contention effects.")
    print()
    print(f"  Raw JSON results: {results_dir}/")
    print("  Re-analyse without re-benchmarking:")
    print(f"    python3 analyse_combined_results.py {results_dir}")
    print()


if __name__ == "__main__":
    main()
