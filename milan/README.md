# Milan Roy (241110042) — JBD2 Journaling Mode Characterization

## What this folder contains

A reproducible benchmark harness that quantifies the performance cost
of ext4's journaling modes (`ordered`, `journal`, `writeback`,
`nojournal`) on a real NVMe partition under two complementary
workloads:

1. **WAL workload** (`wal_workload_no_fc/`) — single-threaded
   sync-heavy write loop with `fdatasync()` after every write,
   parameterized by block size. This is the classic database WAL
   pattern: latency-sensitive, small writes.
2. **Combined workload** (`combined_workload_no_fc/`) — runs the WAL
   pattern concurrently with a bulk 1 MB sequential write stream,
   forcing both workloads to contend for journal resources for the
   full window. This mirrors a database doing WAL writes while a
   reporting query streams large data.

Both harnesses run each journaling mode multiple times, attach
`bpftrace` (`trace_jbd2.bt`) to capture per-commit JBD2 events, and
emit mean ± stddev tables for IOPS, latency (avg / p50 / p99 / p99.9 /
p99.99), throughput, and the write/fsync latency split.

Fast commit was deliberately **off** for these runs to characterize
the baseline JBD2 cost; the suffix `no_fc` denotes that.

## Layout

```
milan/
├── README.md                                ← this file
└── benchmarks/
    ├── wal_workload_no_fc/
    │   ├── run_wal_analysis.sh              ← single-mode WAL benchmark loop
    │   ├── run_repeated.sh                  ← N-run wrapper around the above
    │   ├── analyse_wal_results.py           ← parse one run
    │   ├── analyse_averaged_results.py      ← aggregate N runs into mean ± stddev
    │   ├── wal_workload.fio                 ← fio job: bs varies, fsync=1
    │   ├── trace_jbd2.bt                    ← bpftrace JBD2 commit-path probe
    │   ├── sync_heavy_bs={4,8,16,32,64}k.txt   ← averaged result tables, fsync-heavy
    │   └── seq_write_bs={128,256,512,1m}k.txt  ← averaged result tables, sequential
    └── combined_workload_no_fc/
        ├── run_combined_analysis.sh         ← concurrent WAL + sequential benchmark
        ├── run_repeated.sh                  ← N-run wrapper
        ├── analyse_combined_results.py
        ├── analyse_averaged_results.py
        ├── wal_workload.fio                 ← fio job: WAL pattern
        ├── seq_write.fio                    ← fio job: bulk sequential
        ├── trace_jbd2.bt                    ← bpftrace probe (covers both fios)
        └── combined_workload_no_fc_*.txt    ← averaged result tables
```

## Reproduce

> **Hardware needed:** an empty ext4-formattable NVMe partition. The
> scripts default to `/dev/nvme0n1p6` mounted at
> `/media/milan-roy/test_ext4`; edit the `DEVICE` and `MOUNT_POINT`
> variables at the top of each `run_*.sh` for your machine.
>
> **Software needed:** `fio`, `bpftrace`, `tune2fs`, Python 3, `numpy`.

```bash
cd "MTech Rocks/milan/benchmarks/wal_workload_no_fc"
sudo bash run_repeated.sh 5    # 5 runs × 4 modes × ~140 s each ≈ 50 min
# results land under wal_workload_no_fc/results/<timestamp>/
# averaged tables also printed and saved under results/averaged_<timestamp>.txt
```

```bash
cd "MTech Rocks/milan/benchmarks/combined_workload_no_fc"
sudo bash run_repeated.sh 3    # 3 runs × 3 modes × longer per run
```

## Headline numbers (WAL workload, 1 thread, 100 MB, fsync after every write, 4K)

| Mode      | IOPS    | Bandwidth  | p50 lat  | p99 lat   | p99.99 lat   | IOPS penalty vs nojournal |
|-----------|---------|------------|----------|-----------|--------------|---------------------------|
| ordered   | 185 ± 4 | 0.72 MB/s  | 5.12 ms  | 8.70 ms   | 159.86 ms    | 51.3% |
| journal   | 179 ± 3 | 0.70 MB/s  | 5.25 ms  | 10.13 ms  | 147.79 ms    | 52.9% |
| writeback | 185 ± 2 | 0.72 MB/s  | 5.15 ms  | 8.94 ms   | 107.93 ms    | 51.4% |
| nojournal | 381 ± 3 | 1.49 MB/s  | 2.41 ms  | 7.69 ms   | 72.74 ms     | baseline |

Take-away: on a small-block, fsync-heavy WAL workload, journaling
roughly halves IOPS regardless of mode, but the p99 latency penalty
is mild (≤30%). The fdatasync portion of total latency is **99.6%**
across all journaled modes — i.e. journaling overhead is dominated by
commit synchronization, not by the actual write.

Full per-block-size tables for both workloads are in the `*.txt`
files alongside the scripts.

## Notes for evaluators

- The `bpftrace` script (`trace_jbd2.bt`) hooks `jbd2_journal_*`
  tracepoints to count commits, measure commit duration, and record
  per-commit blocks written. The output is captured into the run
  directory and parsed by `analyse_*_results.py`.
- The fio job files use `runtime=` not `size=`, so each mode runs for
  the same wall time and one mode finishing early does not skew the
  comparison.
- Fast commit is intentionally not enabled here. The companion fast
  commit work is in `suyamoon/` (C3 inline xattr, C4 fallocate range)
  and `sahil/` (lockless wait-commit module).
