# Fragmented Journal Map (FJM) for ext4

## What this folder contains

A loadable kernel module (`ext4_tracker`) that adds a new ext4
mount option, `-o fjm`. With the option set, JBD2 stops using its
strict ring-buffer block allocator and instead picks any free block
in the journal area using an in-memory bitmap. This lets a
transaction's blocks land in non-contiguous slots, so the journal
keeps useful free space even when the ring buffer would have
reported "out of journal space".

Two small patches against in-tree JBD2 (`First.patch`,
`Second.patch`) add the callback hooks the module needs. The full
allocator and on-disk index live inside the out-of-tree module
itself.

## Layout

```
Fragmeted_Jounral_Map_Optimization/
├── README.md                            ← this file
├── patches/
│   ├── First.patch                      ← jbd2/journal.c + include/linux/jbd2.h: callback hooks
│   └── Second.patch                     ← jbd2/transaction.c: txn-start hook
├── module/
│   └── ext4_tracker.tar.gz              ← out-of-tree module source (extract into fs/)
├── benchmarks/
│   ├── fjm_full_benchmark.sh            ← single full benchmark across 6 modes
│   ├── run_repeated_benchmark.sh        ← N-run wrapper for stability check
│   ├── wal_workload.fio                 ← fio WAL job (100 MB, fsync per write)
│   └── README.me                        ← workload notes
├── results/
│   └── logs/                            ← raw benchmark logs (single + 5-run aggregate)
└── report_src/
    ├── report.tex                       ← LaTeX writeup of the design
    └── upstream_README.md               ← original repo README, kept for reference
```

## Setup Instructions

### Hardware Requirements

| Resource | Minimum | Used in This Artifact |
|----------|---------|----------------------|
| CPU cores | 2 | 4 cores recommended |
| RAM | 4 GB | 8 GB recommended |
| Root partition (`/`) | 20 GB | Standard Ubuntu install |
| Test device | 2 GB | `/dev/sdb` — 5.1 GB used |
| GPU | Not required | — |

> **Test Device**: All benchmarks write to `/dev/sdb` (a dedicated 5.1 GB block device / virtual disk). **Do not use a partition that contains your OS or important data.** The scripts call `mkfs.ext4` on this device, wiping it completely before every run.

### Operating System

- **Ubuntu 22.04 LTS** (Server or Desktop)
- Kernel: Linux 6.1.4 (compiled from source — see below)

### Software Dependencies

Install all dependencies before building:

```bash
sudo apt-get update
sudo apt-get install -y fio blktrace git
```

| Tool | Purpose |
|------|---------|
| `fio` | WAL benchmark workload generator |
| `blktrace` / `blkparse` | Physical write byte measurement (WAF calculation) |
| `git` | Applying kernel patches (`git apply`) |

## Getting Started / Linux Kernel Compilation

```bash
# 1. Apply the two JBD2 patches to your linux-6.1.4 source tree
cd /path/to/linux-6.1.4
git apply /path/to/MTech\ Rocks/Fragmeted_Jounral_Map_Optimization/patches/First.patch
git apply /path/to/MTech\ Rocks/Fragmeted_Jounral_Map_Optimization/patches/Second.patch

# 2. Extract the module source into fs/
tar xzf /path/to/MTech\ Rocks/Fragmeted_Jounral_Map_Optimization/module/ext4_tracker.tar.gz -C fs/

# 3. Start from the current kernel config
cp /boot/config-$(uname -r) .config
make olddefconfig

# 4. Enable the module in menuconfig:
#    File systems -> "Ext4 TRACKER filesystem" -> M
make menuconfig

# 5. Build the kernel and the module
make -j$(nproc)
sudo make modules_install
sudo make install
sudo reboot     # boot into the rebuilt 6.1.4

# After reboot:
sudo insmod fs/ext4_tracker/ext4_tracker.ko
cat /proc/filesystems | grep ext4_tracker   # should list ext4_tracker

# 5. Run the benchmark suite
cd /path/to/MTech\ Rocks/Fragmeted_Jounral_Map_Optimization/benchmarks
sudo bash fjm_full_benchmark.sh             # ~4-6 minutes, 6 modes
# or for stability:
sudo bash run_repeated_benchmark.sh 5       # ~20-30 minutes
```

## Headline numbers (5-run average, 100 MB WAL workload, fio fsync=1)

| Mode             | BW (KB/s) | IOPS  | P99 (ms) | Phys (MB) | WAF    |
|------------------|----------:|------:|---------:|----------:|-------:|
| ordered_std      | 2674      | 334.3 | 0.409    | 250.0     | 2.50×  |
| **ordered_fjm**  | **2827**  | 353.5 | **0.053**| 250.0     | 2.50×  |
| journal_std      | 2919      | 364.9 | 0.302    | 350.0     | 3.50×  |
| journal_fjm      | 2301      | 287.7 | 0.424    | 361.3     | 3.61×  |
| writeback_std    | 2772      | 346.6 | 0.054    | 250.0     | 2.50×  |
| writeback_fjm    | 2715      | 339.5 | 0.049    | 250.0     | 2.50×  |

Take-away:

- In `ordered` mode, FJM cuts P99 latency from 0.409 ms to 0.053 ms
  (about 8× lower) with the same write amplification factor (WAF).
- In `writeback` mode, P99 is roughly the same as the standard path.
- In `journal` mode (full data journaling), FJM adds a small WAF
  overhead (3.50× → 3.61×) because the on-disk FJM index block
  itself is journaled. Bandwidth in this mode goes down on the
  current implementation; this is a known cost of writing the index.
- WAF stays stable across five repeated runs in every mode (the
  variance is in bandwidth/IOPS, not in WAF), which is what one
  would expect from an allocator change that does not alter what
  data ends up on disk.

## Assumptions and Unsupported Features

### Assumptions

- The journal size is fixed at format time (`mkfs.ext4 -J size=N`). FJM reads `j_first`/`j_last` from JBD2 at mount time and never changes the bitmap size afterwards.
- The test device (`/dev/sdb`) is dedicated. Scripts call `mkfs.ext4 -F` (force-format) without confirmation.
- The module is always loaded **before** mounting. Mounting as `ext4_tracker` without the module loaded will fail with `unknown filesystem type`.

### Unsupported Features

| Feature | Status | Notes |
|---------|--------|-------|
| Crash Recovery / Replay | Not implemented | On-disk index (Phase 2A) is written but the JBD2 recovery path does not yet read it. After a crash, the standard JBD2 replay runs instead. |

## Features / Functionalities Supported

### Feature 1 — Fragmented Block Allocation (Phase 1)

FJM intercepts JBD2's block allocation via a callback hook (`j_alloc_block_callback`). Instead of the standard ring-buffer next-block selection, FJM uses an in-memory **free-block bitmap** to find any available journal block across the entire journal area, regardless of physical contiguity.

**Mount option**: `-t ext4_tracker -o fjm`

### Feature 2 — Checkpoint Freeing Hook (Phase 1.5)

When JBD2 checkpoints a committed transaction to the main filesystem and frees it, FJM's `j_free_txn_callback` is invoked. FJM scans the transaction's block list and clears the corresponding bitmap bits, recycling those journal blocks for future transactions.

**Observable evidence**: `dmesg | grep "FJM: Freed"` shows recycling events.

### Feature 3 — Static Index Block Array (Phase 2A)

The on-disk FJM index (which maps journal blocks to transactions for crash recovery) is stored across a dynamically calculated array of reserved journal blocks, not just a single block. For a 64 MB journal, **16 index blocks** are reserved, supporting up to `255 + 15×256 = 4,095` fragment entries — enough to cover the entire journal.

**Observable evidence**: `dmesg | grep "FJM: initialised"` shows `index_blocks=N`.

### Feature Test Matrix

| Feature | Test Script | Parameters | Objective | Expected Outcome |
|---------|------------|------------|-----------|-----------------|
| Multi-block index | `fjm_full_benchmark.sh` | 100MB WAL, data=journal | On-disk index covers >255 blocks | `FJM: initialised … index_blocks=16` in dmesg; no truncation warning |
| WAF comparison | `fjm_full_benchmark.sh` | 6 modes × 100MB | FJM WAF ≈ standard WAF | `journal_std=3.50x`, `journal_fjm=3.59–3.62x` |
| Multi-run stability | `run_repeated_benchmark.sh 5` | 5 full runs | WAF is consistent across runs | WAF stable; BW/IOPS/P99 vary ±15% (expected OS jitter) |

## Detailed Evaluation

### Experiment 1 — Single Full-Mode Benchmark

| Field | Details |
|-------|---------|
| **Purpose** | Measure WAF, bandwidth, IOPS, and P99 latency across all 3 journaling modes with and without FJM |
| **Script** | `fjm_full_benchmark.sh` |
| **How to run** | `sudo bash fjm_full_benchmark.sh` |
| **Estimated runtime** | ~40–60 minutes (6 runs × ~7 min each) |
| **Expected result** | `journal_std` WAF = 3.50x, `journal_fjm` WAF ≈ 3.59–3.62x. `ordered`/`writeback` WAF = 2.50x for both std and fjm |
| **Actual result location** | Printed table at end of script. Raw JSON in `fjm_full_results/<timestamp>/wal_*.json` |

### Experiment 2 — Repeated Multi-Run Benchmark (Statistical Stability)

| Field | Details |
|-------|---------|
| **Purpose** | Confirm WAF stability across 5 independent runs; observe bandwidth/latency variance |
| **Script** | `run_repeated_benchmark.sh` |
| **How to run** | `sudo bash run_repeated_benchmark.sh 5` |
| **Estimated runtime** | ~3.5–5 hours (5 × ~45–60 min) |
| **Expected result** | WAF stable (2.50x / 3.50x / 3.59–3.62x). Bandwidth varies ±15% across runs (OS scheduling jitter) |
| **Actual result location** | Console (results table per run only). Full logs in `repeated_run_logs/<timestamp>/run_N.log` |

### Experiment 3 — Heavy FJM Workload (Functional Diversity Test)

| Field | Details |
|-------|---------|
| **Purpose** | Stress-test FJM under diverse I/O patterns: parallel metadata, sequential data, random access, mixed |
| **Script** | `test_fjm_workload.sh` |
| **How to run** | `sudo bash test_fjm_workload.sh` |
| **Estimated runtime** | ~10 minutes |
| **Expected result** | No filesystem errors. `dmesg` shows continuous `FJM: Allocated` and `FJM: Freed` messages throughout |
| **Actual result location** | Console output + `sudo dmesg \| grep "FJM:"` |

## Interpreting Results

| Metric | Definition |
|--------|-----------|
| **BW (KB/s)** | Write bandwidth — higher is better |
| **IOPS** | Write operations per second — higher is better |
| **P99 (ms)** | 99th percentile write latency — lower is better. Represents worst-case fsync commit time experienced by 1 in 100 writes |
| **Logical (MB)** | Bytes written by the application (always 100MB) |
| **Physical (MB)** | Bytes actually written to disk (measured by blktrace) — includes journal overhead |
| **WAF** | Write Amplification Factor = Physical / Logical. The journal overhead multiplier |

## Files Modified in the Kernel

| File | How to Apply | Nature of Change |
|------|-------------|------------------|
| `fs/ext4_tracker/fjmap.c` | `ext4_tracker.tar.gz` | **New file** — complete FJM core implementation |
| `fs/ext4_tracker/fjmap.h` | `ext4_tracker.tar.gz` | **New file** — on-disk and in-memory structure definitions |
| `fs/ext4_tracker/ext4.h` | `ext4_tracker.tar.gz` | **New file** — ext4 superblock info with `EXT4_MOUNT2_FJM` flag and `s_fjmap` field |
| `fs/ext4_tracker/super.c` | `ext4_tracker.tar.gz` | **New file** — mount option parsing (`fjm`/`nofjm`); `ext4_fjmap_init`/`destroy` call sites |
| `fs/ext4_tracker/ext4_jbd2.c` | `ext4_tracker.tar.gz` | **New file** — `ext4_fjmap_begin_txn` and `ext4_fjmap_commit_txn` hooks |
| `fs/ext4_tracker/Makefile` | `ext4_tracker.tar.gz` | **New file** — builds `ext4_tracker.ko` with `fjmap.o` |
| `include/linux/jbd2.h` | `First.patch` | Added `j_alloc_block_callback` and `j_free_txn_callback` to `struct journal_s` |
| `fs/jbd2/journal.c` | `First.patch` | Added `j_alloc_block_callback` call in `jbd2_journal_next_log_block` |
| `fs/jbd2/transaction.c` | `Second.patch` | Added `j_free_txn_callback` call in `jbd2_journal_free_transaction` |
