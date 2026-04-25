#!/usr/bin/env python3
"""
analyse_wal_results.py

Parses fio JSON output from the WAL journaling benchmark and produces
a comprehensive comparison of ordered / journal / writeback modes.

Called automatically by run_wal_analysis.sh after benchmarking, or run
manually to re-analyse previously saved results without re-benchmarking:

  Usage:   python3 scripts/analyse_wal_results.py <results_dir>
  Example: python3 scripts/analyse_wal_results.py results/wal/20260228_164708
"""

import json
import os
import sys


# ── Data loading ──────────────────────────────────────────────────────────────

def load_results(results_dir):
    modes = ["ordered", "journal", "writeback", "nojournal"]
    data = {}
    for mode in modes:
        path = os.path.join(results_dir, f"wal_{mode}.json")
        try:
            with open(path) as f:
                data[mode] = json.load(f)
        except FileNotFoundError:
            print(f"ERROR: Result file not found: {path}")
            sys.exit(1)
    return data, modes


def extract_metrics(data, modes):
    """Pull every field we need from the fio JSON into a flat dict per mode."""
    metrics = {}
    for mode in modes:
        job   = data[mode]["jobs"][0]
        write = job["write"]
        clat_ns = write["clat_ns"]

        # sync_block: fdatasync() latency reported separately by fio.
        sync_block = job.get("sync", {})
        if sync_block and "lat_ns" in sync_block and sync_block["lat_ns"].get("mean", 0) > 0:
            sync_ns = sync_block["lat_ns"]
        else:
            sync_ns = {"mean": 0, "percentile": {k: 0 for k in clat_ns.get("percentile", {})}}

        # Total latency = write() + fdatasync() combined.
        # fio reports write.lat_ns (slat+clat) and sync.lat_ns separately.
        # We sum them to get the true application round-trip per 4KB WAL write.
        write_lat_ns = write["lat_ns"]
        write_pct = write_lat_ns.get("percentile", {})
        sync_pct  = sync_ns.get("percentile", {})

        # disk_util covers the entire block device (e.g. nvme0n1), which
        # includes all partitions on that device, not just the test partition.
        disk = (data[mode].get("disk_util") or [{}])[0]

        runtime_s = job["job_runtime"] / 1000   # fio reports in ms

        metrics[mode] = {
            # ── Throughput ─────────────────────────────────────────────────────
            "iops"        : write["iops"],
            "bw_mbs"      : write["bw"] / 1024,          # KB/s → MB/s
            "total_gb"    : write["io_bytes"] / (1024**3),
            "total_ios"   : write["total_ios"],
            "logical_mb"  : write["io_bytes"] / (1024**2),

            # ── IOPS variability ───────────────────────────────────────────────
            "iops_min"    : write["iops_min"],
            "iops_max"    : write["iops_max"],
            "iops_stddev" : write["iops_stddev"],

            # ── Total latency (write + fdatasync) in ms ───────────────────────
            "avg_ms"   : (write_lat_ns["mean"] + sync_ns["mean"])                                  / 1_000_000,
            "p50_ms"   : (float(write_pct.get("50.000000", 0)) + float(sync_pct.get("50.000000", 0))) / 1_000_000,
            "p99_ms"   : (float(write_pct.get("99.000000", 0)) + float(sync_pct.get("99.000000", 0))) / 1_000_000,
            "p999_ms"  : (float(write_pct.get("99.900000", 0)) + float(sync_pct.get("99.900000", 0))) / 1_000_000,
            "p9990_ms" : (float(write_pct.get("99.990000", 0)) + float(sync_pct.get("99.990000", 0))) / 1_000_000,

            # ── Latency split: write() vs fdatasync() in µs ────────────────────
            # write_us  = clat_ns.mean  → time for write() to return to page cache
            # sync_us   = sync_ns.mean  → time for fdatasync() to return from disk
            "write_us" : clat_ns["mean"] / 1_000,
            "sync_us"  : sync_ns["mean"] / 1_000,

            # ── CPU & kernel ───────────────────────────────────────────────────
            "sys_cpu" : job.get("sys_cpu", 0),
            "usr_cpu" : job.get("usr_cpu", 0),
            "ctx"     : job.get("ctx", 0),

            # ── Device-level I/O (entire nvme0n1, includes other partitions) ───
            "phys_write_ios"    : disk.get("write_ios", 0),
            "phys_write_merges" : disk.get("write_merges", 0),
            "phys_read_sectors" : disk.get("read_sectors", 0),
            "disk_util_pct"     : disk.get("util", 0),
            "runtime_s"         : runtime_s,
        }
    return metrics


def load_blktrace_bytes(results_dir, modes):
    """Read physical write bytes from blkparse summary files.

    Returns a dict {mode: int_bytes} for modes that have blktrace data,
    or {mode: None} for modes where the file is absent (older result sets).
    """
    phys = {}
    for mode in modes:
        path = os.path.join(results_dir, f"phys_write_bytes_{mode}.txt")
        try:
            with open(path) as f:
                phys[mode] = int(f.read().strip() or 0)
        except (FileNotFoundError, ValueError):
            phys[mode] = None
    return phys


def load_jbd2_trace(results_dir, modes):
    """Parse bpftrace JBD2 output files into per-mode metric dicts.

    Each file contains a KEY=VALUE block between sentinel lines:
        --- JBD2_TRACE_RESULTS_BEGIN ---
        KEY=VALUE
        ...
        --- JBD2_TRACE_RESULTS_END ---

    Returns {mode: dict_of_int_values} for modes with trace files,
    {mode: None} for modes where the file is absent (older result sets or
    bpftrace not available). nojournal mode produces a file with all-zero
    values because JBD2 is disabled -- the dict is present but zero-filled.
    """
    import re
    jbd2 = {}
    for mode in modes:
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


# ── Individual table printers ─────────────────────────────────────────────────

def table1_throughput(metrics, modes, W):
    print()
    print("  TABLE 1: Throughput")
    print(f"  {'Mode':<12}  {'IOPS':>8}  {'Bandwidth':>12}  {'Data Written':>14}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        print(f"  {mode:<12}  {m['iops']:>8.0f}  {m['bw_mbs']:>9.2f} MB/s"
              f"  {m['total_gb']:>11.2f} GB")


def table2_commit_latency(metrics, modes, W):
    print()
    print("  TABLE 2: Total I/O Latency  (write() + fdatasync() round-trip per 4KB)")
    print(f"  {'Mode':<12}  {'Total (s)':>10}  {'Avg':>10}  {'p50':>10}  {'p99':>10}  {'p99.9':>10}  {'p99.99':>10}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        print(f"  {mode:<12}  {m['runtime_s']:>8.2f}s   {m['avg_ms']:>8.2f}ms  {m['p50_ms']:>8.2f}ms"
              f"  {m['p99_ms']:>8.2f}ms  {m['p999_ms']:>8.2f}ms  {m['p9990_ms']:>8.2f}ms")


def table3_overhead(metrics, modes, W):
    nj = metrics["nojournal"]
    print()
    print("  TABLE 3: Performance Overhead  (relative to nojournal baseline)")
    print(f"  {'Mode':<12}  {'IOPS penalty':>14}  {'Lat overhead':>14}  {'p99 overhead':>14}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        if mode == "nojournal":
            print(f"  {mode:<12}  {'baseline':>14}  {'baseline':>14}  {'baseline':>14}")
        else:
            iops_pen = (nj["iops"] - m["iops"]) / nj["iops"] * 100
            lat_over = (m["avg_ms"] - nj["avg_ms"]) / nj["avg_ms"] * 100
            p99_over = (m["p99_ms"] - nj["p99_ms"]) / nj["p99_ms"] * 100
            print(f"  {mode:<12}  {iops_pen:>12.1f}%  {lat_over:>12.1f}%  {p99_over:>12.1f}%")


def table4_iops_variability(metrics, modes, W):
    print()
    print("  TABLE 4: IOPS Variability & Consistency")
    print(f"  {'Mode':<12}  {'Min':>8}  {'Max':>8}  {'Std Dev':>9}  {'CV (%)':>8}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        cv = (m["iops_stddev"] / m["iops"] * 100) if m["iops"] > 0 else 0
        print(f"  {mode:<12}  {m['iops_min']:>8.0f}  {m['iops_max']:>8.0f}"
              f"  {m['iops_stddev']:>9.1f}  {cv:>7.1f}%")
    print()
    print("  CV = std dev / mean (coefficient of variation). Lower = more predictable.")
    print("  A high CV means IOPS fluctuates wildly between measurement windows,")
    print("  caused by periodic journal checkpoint stalls that temporarily block all")
    print("  new writes. journal mode's serialised commits act as a natural rate")
    print("  limiter -- lower CV despite lowest average throughput.")


def table5_latency_split(metrics, modes, W):
    print()
    print("  TABLE 5: I/O Latency Breakdown -- write() vs fdatasync()")
    print(f"  {'Mode':<12}  {'write() avg':>13}  {'fdatasync() avg':>16}  {'fdatasync share':>16}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        total_us  = m["write_us"] + m["sync_us"]
        fsync_pct = (m["sync_us"] / total_us * 100) if total_us > 0 else 0
        print(f"  {mode:<12}  {m['write_us']:>10.0f} µs  {m['sync_us']:>13.0f} µs  {fsync_pct:>14.1f}%")
    print()
    print("  write() copies data into the page cache and returns in microseconds.")
    print("  fdatasync() blocks until JBD2 commits the journal transaction to disk.")
    print("  Table 2 shows the combined total (write + fdatasync); this table")
    print("  breaks it down to show what fraction of the total is JBD2 commit time.")


def table6_cpu_efficiency(metrics, modes, W):
    print()
    print("  TABLE 6: CPU & Kernel Efficiency")
    print(f"  {'Mode':<12}  {'sys_cpu':>8}  {'usr_cpu':>8}  {'total_cpu':>10}  {'ctx sw / IO':>12}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        total_cpu  = m["sys_cpu"] + m["usr_cpu"]
        ctx_per_io = m["ctx"] / m["total_ios"] if m["total_ios"] > 0 else 0
        print(f"  {mode:<12}  {m['sys_cpu']:>7.2f}%  {m['usr_cpu']:>7.2f}%"
              f"  {total_cpu:>9.2f}%  {ctx_per_io:>11.2f}")
    print()
    print("  sys_cpu: kernel time (VFS, JBD2 commit thread, block layer).")
    print("  ordered uses the most sys_cpu despite mid-range throughput because")
    print("  JBD2's pre-flush background thread runs continuously, consuming CPU")
    print("  to drain dirty data pages ahead of each metadata commit.")
    print("  ctx sw / IO: wakeups per commit (JBD2 kthread + fio thread + block layer).")
    print("  journal mode wakes more kernel threads per IO due to data journaling.")


def table7_device_io(metrics, modes, W):
    print()
    print("  TABLE 7: Device-Level I/O Statistics")
    print("  NOTE: disk_util covers the entire nvme0n1 device (all partitions),")
    print("        not just the test partition. Root fs I/O is included.")
    print()
    print(f"  {'Mode':<12}  {'Phys IOPS':>10}  {'Merge ratio':>12}  {'Reads (MB)':>11}  {'Disk util%':>11}")
    print("  " + "-" * (W - 2))
    for mode in modes:
        m = metrics[mode]
        phys_iops  = m["phys_write_ios"] / m["runtime_s"] if m["runtime_s"] > 0 else 0
        total_reqs = m["phys_write_ios"] + m["phys_write_merges"]
        merge_pct  = (m["phys_write_merges"] / total_reqs * 100) if total_reqs > 0 else 0
        read_mb    = m["phys_read_sectors"] * 512 / (1024**2)
        print(f"  {mode:<12}  {phys_iops:>10.0f}  {merge_pct:>10.1f}%"
              f"  {read_mb:>10.1f}  {m['disk_util_pct']:>10.1f}%")
    print()
    print("  Phys IOPS: actual disk operations/s after kernel I/O merging.")
    print("  Merge ratio: fraction of write requests coalesced by the I/O scheduler")
    print("  before they reach the disk. journal mode gets the highest ratio because")
    print("  lower logical IOPS gives the block layer more time to merge neighbours.")
    print("  Reads (MB): unexpected reads during this write-only benchmark. Large")
    print("  values indicate OS background activity on other partitions of nvme0n1.")


def table8_write_amplification(metrics, phys_bytes, nojournal_phys, modes, W):
    """Table 8: Journaling Write Amplification Factor from blktrace data.

    WAF = physical_bytes_with_journal / physical_bytes_without_journal.
    This isolates the amplification caused by journaling alone, excluding
    base filesystem metadata writes that occur in all modes.

    Falls back to physical/logical ratio if nojournal baseline is absent.
    Silently skipped when no blktrace files exist at all.
    """
    if all(v is None for v in phys_bytes.values()):
        return

    print()
    print("  TABLE 8: Journaling Write Amplification  (blktrace on nvme0n1p6)")
    print("  WAF = physical bytes (with journal) / physical bytes (no journal).")
    print("  Isolates journaling overhead: descriptor blocks, commit blocks,")
    print("  checkpoint I/O -- excluding base filesystem metadata present in all modes.")
    print()

    if nojournal_phys is not None:
        nojournal_mb = nojournal_phys / (1024 ** 2)
        print(f"  No-journal baseline: {nojournal_mb:.4f} MB physical")
        print()
        print(f"  {'Mode':<12}  {'Physical MB':>15}  {'Journaling WAF':>15}")
        print("  " + "-" * (W - 2))
        for mode in modes:
            phys = phys_bytes[mode]
            if phys is None:
                print(f"  {mode:<12}  {'N/A':>15}  {'N/A':>15}")
            else:
                phys_mb = phys / (1024 ** 2)
                waf = phys_mb / nojournal_mb if nojournal_mb > 0 else 0
                print(f"  {mode:<12}  {phys_mb:>14.4f}  {waf:>14.4f}x")
    else:
        # No nojournal baseline -- fall back to physical/logical ratio
        print("  NOTE: No nojournal baseline found. Showing raw physical/logical ratio.")
        print(f"  {'Mode':<12}  {'Logical MB':>14}  {'Physical MB':>15}  {'WAF':>10}")
        print("  " + "-" * (W - 2))
        for mode in modes:
            logical_mb = metrics[mode]["logical_mb"]
            phys = phys_bytes[mode]
            if phys is None:
                print(f"  {mode:<12}  {logical_mb:>13.4f}  {'N/A':>15}  {'N/A':>10}")
            else:
                phys_mb = phys / (1024 ** 2)
                waf = phys_mb / logical_mb if logical_mb > 0 else 0
                print(f"  {mode:<12}  {logical_mb:>13.4f}  {phys_mb:>14.4f}  {waf:>9.4f}x")
    print()
    print("  ordered  ~4x: data(1x) + descriptor(1x) + metadata(1x) + commit(1x).")
    print("  journal  ~5x: same as ordered + checkpoint writes data to final location(1x).")
    print("  writeback ~4x: identical journal traffic to ordered for fdatasync workloads.")


def table9_jbd2_activity(jbd2, metrics, modes, W):
    """Table 9: JBD2 journal function call tracing (bpftrace).

    Three sub-tables:
      9A: Call counts per JBD2 tracepoint
      9B: Average time per commit pipeline stage
      9C: Per-transaction workload and % of benchmark time spent in JBD2

    Silently skipped when no jbd2 trace files exist (older result sets or
    bpftrace unavailable). nojournal shows '(disabled)' -- JBD2 not loaded.
    """
    if all(v is None for v in jbd2.values()):
        return

    def g(d, key):
        """Safe int getter; returns 0 for None dict or missing key."""
        if d is None:
            return 0
        return d.get(key, 0)

    print()
    print("=" * W)
    print("  TABLE 9: JBD2 Journal Function Call Tracing  (bpftrace)")
    print("=" * W)
    print("  All tracepoints filtered to nvme0n1p6 only (no root-fs noise).")
    print("  nojournal: JBD2 disabled -- no journal activity.")

    # ── 9A: Tracepoint call counts ────────────────────────────────────────────
    print()
    print("  TABLE 9A: Tracepoint Call Counts")
    col_w = 13
    print(f"  {'Tracepoint':<30}" + "".join(f"{m:>{col_w}}" for m in modes))
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
        for mode in modes:
            d = jbd2[mode]
            if d is None:
                row += f"{'N/A':>{col_w}}"
            elif mode == "nojournal":
                row += f"{'(disabled)':>{col_w}}"
            else:
                row += f"{g(d, key):>{col_w},}"
        print(row)

    # ── 9B: Average time per commit pipeline stage ────────────────────────────
    print()
    print("  TABLE 9B: Average Time per Commit Pipeline Stage")
    print("  locking/flushing/logging/end-to-end: bpftrace nanosecond timestamps.")
    print("  wait/request_delay/running: kernel jiffies (ms, HZ=1000) -- no bpftrace equivalent.")
    print()
    print(f"  {'Stage':<28}" + "".join(f"{m:>{col_w}}" for m in modes))
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

    rows_9b = [
        # (label, primary_fn, fallback_fn)
        # Primary: bpftrace ns timestamps (most accurate).
        # Fallback: kernel jiffies from jbd2_run_stats (1ms resolution).
        # wait/request_delay/running have no bpftrace equivalent -- jiffies only.
        ("wait (handle blocked)",    lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_WAIT_MS"),     None),
        ("request_delay",            lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_REQDELAY_MS"), None),
        ("running (txn open)",       lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_RUNNING_MS"),  None),
        ("locking (txn close)",      lambda d, m: tp_avg_ms(d, m, "LOCKING_TOTAL_US", "LOCKING_COUNT"),
                                     lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_LOCKED_MS")),
        ("flushing (data writeback)",lambda d, m: tp_avg_ms(d, m, "FLUSHING_TOTAL_US", "FLUSHING_COUNT"),
                                     lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_FLUSHING_MS")),
        ("logging (journal write)",  lambda d, m: tp_avg_ms(d, m, "LOGGING_TOTAL_US", "LOGGING_COUNT"),
                                     lambda d, m: run_stats_avg_ms(d, m, "RUN_STATS_SUM_LOGGING_MS")),
        ("end-to-end commit",        lambda d, m: tp_avg_ms(d, m, "COMMIT_TOTAL_US", "COMMIT_COUNT"),  None),
    ]

    for label, primary_fn, fallback_fn in rows_9b:
        row = f"  {label:<28}"
        for mode in modes:
            d = jbd2[mode]
            if mode == "nojournal" or d is None:
                row += f"{'(disabled)':>{col_w}}"
                continue
            val = None
            if primary_fn is not None:
                val = primary_fn(d, mode)
            if (val is None) and (fallback_fn is not None):
                val = fallback_fn(d, mode)
            if val is None:
                row += f"{'N/A':>{col_w}}"
            else:
                row += f"{val:>{col_w - 2}.3f}ms"
        print(row)

    # ── 9C: Per-transaction workload and JBD2 time budget ─────────────────────
    print()
    print("  TABLE 9C: Per-Transaction Workload & JBD2 Time Budget")
    print()
    print(f"  {'Metric':<34}" + "".join(f"{m:>{col_w}}" for m in modes))
    print("  " + "-" * (W - 2))

    def fmt_ratio(d, mode, num_key, den_key, decimals=1):
        if d is None or mode == "nojournal":
            return "(disabled)"
        n = g(d, den_key)
        return f"{g(d, num_key) / n:.{decimals}f}" if n else "0"

    def fmt_pct(d, mode, total_us_key, runtime_s):
        if d is None or mode == "nojournal":
            return "(disabled)"
        runtime_us = runtime_s * 1_000_000
        commit_us  = g(d, total_us_key)
        pct = (commit_us / runtime_us * 100) if runtime_us > 0 else 0
        return f"{pct:.1f}%"

    rows_9c = [
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
         lambda d, m: fmt_pct(d, m, "COMMIT_TOTAL_US", metrics[m]["runtime_s"])),
        ("lock_buffer_stall total (ms)",
         lambda d, m: "(disabled)" if (d is None or m == "nojournal")
                      else str(g(d, "LOCK_BUFFER_STALL_TOTAL_MS"))),
    ]

    for label, fn in rows_9c:
        row = f"  {label:<34}"
        for mode in modes:
            val = fn(jbd2[mode], mode)
            row += f"{val:>{col_w}}"
        print(row)

    print()
    print("  9A: jbd2_submit_inode_data fires only in journal mode (data journaling).")
    print("      handle_start count ≈ 2× IOPS in journal mode (extra handle for data).")
    print("  9B: flushing dominates ordered (data must reach disk before metadata commit).")
    print("      flushing ≈ 0 in writeback (no ordering); journal (data goes through log).")
    print("      logging dominates journal mode (every 4KB block written to journal).")
    print("  9C: blocks_logged > blocks_dirtied in journal mode (data logged twice).")
    print("      JBD2 commit % = fraction of wall-clock time in the JBD2 commit pipeline.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    results_dir = sys.argv[1]
    if not os.path.isdir(results_dir):
        print(f"ERROR: Results directory not found: {results_dir}")
        sys.exit(1)

    data, modes  = load_results(results_dir)
    metrics      = extract_metrics(data, modes)
    phys_bytes   = load_blktrace_bytes(results_dir, modes)
    jbd2         = load_jbd2_trace(results_dir, modes)

    # nojournal physical bytes used as WAF baseline in Table 8.
    nojournal_phys = phys_bytes.get("nojournal")

    W = 78

    print()
    print("=" * W)
    print("  WAL WORKLOAD RESULTS  (1 thread · 100 MB · fdatasync after every 4 KB write)")
    print("=" * W)

    table1_throughput(metrics, modes, W)
    table2_commit_latency(metrics, modes, W)
    table3_overhead(metrics, modes, W)
    table4_iops_variability(metrics, modes, W)
    table5_latency_split(metrics, modes, W)
    table6_cpu_efficiency(metrics, modes, W)
    table7_device_io(metrics, modes, W)
    table8_write_amplification(metrics, phys_bytes, nojournal_phys, modes, W)
    table9_jbd2_activity(jbd2, metrics, modes, W)

    print()
    print("=" * W)
    print("  INTERPRETATION GUIDE")
    print("=" * W)
    print()
    print("  TABLE 1  IOPS/bandwidth: how much useful work each mode delivered.")
    print("  TABLE 2  Commit latency: how long each fdatasync() call blocked the app.")
    print("  TABLE 3  Overhead %: performance cost of safety vs writeback baseline.")
    print("  TABLE 4  CV%: IOPS consistency -- journal mode is most predictable (lowest CV%).")
    print("  TABLE 5  Latency split: confirms fdatasync, not write(), is the bottleneck.")
    print("  TABLE 6  CPU: ordered pre-flushing costs more sys_cpu than journal mode.")
    print("  TABLE 7  Phys IOPS/merge: block-device view of each mode's journal traffic.")
    print("  TABLE 8  WAF: journaling overhead = physical(mode) / physical(nojournal baseline).")
    print("  TABLE 9  JBD2 tracing: commit counts, stage timing, % bench time in journaling.")

    print(f"  Raw JSON results: {results_dir}/")
    print("  Re-analyse without re-benchmarking:")
    print(f"    python3 scripts/analyse_wal_results.py {results_dir}")
    print()


if __name__ == "__main__":
    main()
