# Workload Characteristics

## What this folder contains

A reproducible benchmark harness that quantifies the performance cost
of ext4's journaling modes (`ordered`, `journal`, `writeback`,
`nojournal`) on a real NVMe partition under three complementary
workloads:

1. **Sync Heavy Workload** (`Sync Heavy Workload/`) — single-threaded
   write loop with `fsync()` after every write, parameterized by block
   size. This is the classic database WAL pattern: latency-sensitive,
   small writes where every operation forces a journal commit.
2. **Sequential Write Workload** (`Seq Write Workload/`) — single-threaded
   sequential write without per-write fsync, measuring raw journaling
   overhead without the per-write commit penalty. Parameterized by block
   size with larger blocks (128k–1m).
3. **Combined Workload** (`Combined Workload/`) — runs the sync-heavy
   and sequential write fio jobs simultaneously across all four journaling
   modes, forcing both workloads to contend for journal resources
   concurrently.

The Sync Heavy and Sequential Write harnesses run each journaling mode N
times (configurable), attach `blktrace` to capture physical write bytes,
and optionally attach `bpftrace` (`trace_jbd2.bt`) to capture per-commit
JBD2 events. Results are written per run and aggregated by
`analyse_results.py`. The Combined Workload measures throughput and latency
only — it does not use `blktrace` and does not report WAF.

Fast commit was deliberately **off** for these runs to characterize
the baseline JBD2 cost.

## Layout

```
Workload_Characteristics/
├── README.md                                    ← this file
├── Sync Heavy Workload/
│   ├── run_analysis.sh                          ← interactive benchmark: modes × block sizes × N runs
│   ├── sync_heavy_workload.fio                  ← fio job: sequential write, fsync=1 per op
│   ├── trace_jbd2.bt                            ← bpftrace JBD2 commit-path probe
│   ├── analyse_results.py                       ← aggregate N run dirs into mean ± stddev tables
│   └── results/<timestamp>/                     ← per-run output (JSON, blktrace, jbd2 trace, WAF)
├── Seq Write Workload/
│   ├── run_analysis.sh                          ← interactive benchmark: modes × block sizes × N runs
│   ├── seq_write_workload.fio                   ← fio job: sequential write, no fsync
│   ├── trace_jbd2.bt                            ← bpftrace JBD2 commit-path probe
│   └── analyse_results.py                       ← aggregate N run dirs into mean ± stddev tables
└── Combined Workload/
    ├── run_analysis.sh                          ← interactive benchmark: runs sync-heavy + seq-write simultaneously
    ├── sync_heavy_workload.fio                  ← fio job: WAL pattern (fsync=1)
    ├── seq_write_workload.fio                   ← fio job: bulk sequential write
    └── analyse_results.py                       ← aggregate N run dirs into mean ± stddev tables
```

## Hardware Specifications

All experiments were run on bare-metal Ubuntu 24.04.2 LTS with a custom-built
Linux 6.1.4 kernel.

| Component | Details                           |
|-----------|-----------------------------------|
| CPU       | Intel Core i3 (11th Gen), 4 cores |
| RAM       | 8 GB                              |
| Storage   | NVMe SSD                          |
| OS        | Ubuntu 24.04.2 LTS (bare-metal)   |
| Kernel    | Linux 6.1.4 (custom build)        |

When building the kernel, ensure the following flags are set in the config
(required for BTF metadata, kprobes, and debug symbols used by `bpftrace`
and `blktrace`):

```bash
CONFIG_DEBUG_INFO
CONFIG_DEBUG_INFO_BTF
CONFIG_KPROBE_EVENTS
CONFIG_DEBUG_INFO_DWARF5
```
If `bpftrace` or `debugfs` errors occur, set `ENABLE_JBD2_TRACE=0` in the relevant `run_analysis.sh`. JBD2 commit statistics will be omitted from the output tables, but the fio benchmarks will complete normally.

## Reproduce

### Storage

An empty ext4-formattable NVMe partition of 30 GB or more. All scripts
prompt interactively for device and mount point, defaulting to
`/dev/nvme0n1p6` mounted at `/media/milan-roy/test_ext4`. Edit these
at the prompt or change the defaults at the top of each `run_analysis.sh`.

### Software Dependencies

Install once before running any workload:

```bash
sudo apt update
sudo apt install -y fio blktrace bpftrace e2fsprogs python3 python3-numpy
```

### Sync Heavy Workload

This experiment studies how a per-write `fsync()` call affects JBD2
performance — throughput, latency, and write amplification — as block
size is varied. Each write forces an immediate journal commit, isolating
the cost of commit synchronization.

```bash
cd "MTech Rocks/Workload_Characteristics/Sync Heavy Workload"
sudo bash run_analysis.sh
# prompts for device, mount point, size, block sizes (default: 4k 8k 16k 32k 64k), N runs (default: 5)
# results land under results/<timestamp>/
# re-run analysis only:
#   python3 analyse_results.py --block-sizes 4k 8k 16k 32k 64k --size 100m -- results/<dir1> ...
```

Expected runtime: ~20 minutes per run; ~1 hour 40 minutes for the default N=5.
Output: two tables showing throughput, latency, bandwidth,
physical bytes written, and WAF per journaling mode and block size.

### Sequential Write Workload

This experiment studies JBD2 behaviour under a pure sequential write
workload with no blocking `fsync()` calls, allowing the kernel to perform
writeback, commits, and checkpointing freely. This serves as a baseline
to contrast with the sync-heavy results.

```bash
cd "MTech Rocks/Workload_Characteristics/Seq Write Workload"
sudo bash run_analysis.sh
# prompts for device, mount point, size, block sizes (default: 128k 256k 512k 1m), N runs (default: 5)
# results land under results/<timestamp>/
```

Expected runtime: ~40 minutes per run; ~3.5 hours for the default N=5.
Output: two tables showing throughput, latency, bandwidth, physical bytes
written, and WAF per journaling mode and block size.

### Combined Workload

This experiment studies JBD2 under a realistic mixed workload where a
sync-heavy writer (simulating a database WAL) and a bulk sequential writer
run simultaneously, competing for journal resources. This mirrors a database
performing WAL writes while a reporting query streams large amounts of data.

```bash
cd "MTech Rocks/Workload_Characteristics/Combined Workload"
sudo bash run_analysis.sh
# prompts for device, mount point, runtime per mode (default: 120s),
# BS for sync-heavy (default: 8k), BS for seq-write (default: 1m), N runs (default: 5)
# runs both fio jobs simultaneously per mode; results under results/combined_<timestamp>/
# re-run analysis only:
#   python3 analyse_results.py --bs-sync 8k --bs-seq 1m -- results/combined_<dir1> ...
```

Expected runtime: ~8 minutes per run; ~45 minutes for the default N=5.
Output: comparison table for both the sync-heavy and sequential jobs
across all four journaling modes.

## Notes for evaluators

- Each `run_analysis.sh` is fully interactive: it prompts for all
  configurable parameters and prints a summary before proceeding.
- `blktrace` captures physical write bytes per mode/block-size run;
  `blkparse` reduces it to a single byte count in
  `phys_write_bytes_<mode>_bs=<bs>.txt`, which `analyse_results.py`
  uses to compute the write amplification factor (WAF).
- The `bpftrace` script (`trace_jbd2.bt`) hooks JBD2 tracepoints to
  count commits and measure commit duration. It is started alongside
  fio and stopped after fio completes.
- The fio jobs use `size=` (not `time_based`), so each mode writes the
  same number of bytes. The Combined Workload overrides this with
  `--runtime` so both jobs run for the same wall time.
- Fast commit is intentionally not enabled here. The companion fast
  commit work is in `Fast_Commit_Optimization/` (C3 inline xattr,
  C4 fallocate range) and `Lockless_CAS_Optimization/` (lockless
  wait-commit module).
