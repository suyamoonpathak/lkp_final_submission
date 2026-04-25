# Sahil Basia (241110061) — Lockless CAS for `jbd2_log_wait_commit`

## What this folder contains

A loadable kernel module (`jbd2_trace.ko`) that does two things:

1. **Traces** `fsync()` end-to-end and `jbd2_log_wait_commit` separately
   via `kretprobes`, measuring how much of total `fsync()` time is
   spent waiting on the global `j_state_lock`.
2. **Optionally replaces** `jbd2_log_wait_commit` with a lockless
   double-checked-locking + Compare-And-Swap (CAS) implementation when
   loaded with `optimize=1`. The replacement uses `READ_ONCE` for the
   completion check, an atomic CAS gate so only one thread acquires
   the lock to wake the daemon, and `smp_rmb()` to preserve
   data-completion ordering after wakeup.

Tracing on stock JBD2 shows ~78% of `fsync()` lifetime is consumed
inside `jbd2_log_wait_commit` under high concurrency. The CAS
replacement removes most of that contention — see results below.

## Layout

```
sahil/
├── README.md                        ← this file
├── module/
│   ├── jbd2_trace.c                 ← module source (tracer + optimizer)
│   └── Makefile                     ← out-of-tree kbuild Makefile
├── benchmarks/
│   ├── run_evals.sh                 ← scaling matrix: ordered/writeback × opt 0/1 × threads {1,4,8,16} × 3 runs × {fio, sysbench, fs_mark, dbench}
│   ├── master_runner.sh             ← top-level orchestrator
│   ├── dmesg_sampler.sh             ← extracts per-run lock-overhead % from dmesg
│   ├── validation.sh                ← crash-safety / fsck.ext4 -n smoke test
│   └── plot_results.py              ← reads benchmark_results/ and emits clustered bar charts
├── results/
│   ├── benchmark_results/           ← raw per-run JSON / txt (≈340 files)
│   ├── dmesg_logs/                  ← sampled dmesg snippets showing lock-overhead %
│   ├── fs_log.txt                   ← validation.sh output
│   └── optimization_results.png     ← summary chart
└── report_src/
    ├── sahil.tex                    ← subsection-style LaTeX (input into combined report)
    ├── report_latex.tex             ← standalone polished version (twocolumn, abstract, figs)
    └── report_draft.md              ← markdown working draft
```

## Reproduce

> **Target:** Linux 6.1.4 with kernel headers installed (so out-of-tree
> modules can build). The module does not require any in-tree patch.

```bash
# 1. Build the module
cd "MTech Rocks/sahil/module"
make clean && make

# 2. Crash-safety verification (fsck.ext4 -n after concurrent fsync flood)
cd ../benchmarks
sudo ./validation.sh

# 3. Lock-overhead trace (loads opt=0 module, runs fio, dumps dmesg)
sudo ./dmesg_sampler.sh

# 4. Full scaling matrix (≈30-60 minutes; needs an NVMe partition)
sudo ./run_evals.sh   # prompts for /dev/<part> and mount path
                      # writes JSON+txt under sahil/results/benchmark_results/

# 5. Plot
python3 plot_results.py   # writes optimization_results.png
```

## Headline numbers (16 threads, ext4 data=ordered, NVMe)

| Workload                  | Baseline (opt=0) | CAS optimized (opt=1) | Change |
|---|---|---|---|
| Lock contention (fio 4K randwrite, fsync=1) | 14,200 IOPS | 19,850 IOPS | **+40%** |
| Metadata stress (fs_mark create/unlink)     |  8,500 IOPS | 13,100 IOPS | **+54%** |
| Database simulation (sysbench OLTP)         | 21,400 IOPS | 22,900 IOPS | +7%    |
| Sequential streaming (10 MB seq write)      | (SSD-bound, no CPU lock pressure) | (no regression) | ~0% |

Lock overhead measured by the tracer: **~78% of total `fsync()` time** is
spent inside `jbd2_log_wait_commit` on the baseline kernel under heavy
concurrency.

## Notes for evaluators

- The optimization is delivered as an **out-of-tree kernel module that
  uses kretprobes**, not as a patch against `fs/jbd2/`. This makes it
  trivial to load/unload on a stock 6.1.4 kernel with no rebuild.
- `validation.sh` runs `fsck.ext4 -n` after the bombardment to confirm
  the lockless path preserves crash-consistency invariants.
- The `data=journal` mode is intentionally not benchmarked: full
  data-journaling saturates the SSD long before any CPU lock
  contention manifests, so the optimization is invisible there.
