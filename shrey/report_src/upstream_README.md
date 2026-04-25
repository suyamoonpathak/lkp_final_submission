# Fragmented Journal Map (FJM) for ext4 — CS614 Project Artifact

## Overview

This project implements the **Fragmented Journal Map (FJM)**, a kernel-level extension to the ext4 filesystem that allows JBD2 journal transactions to use **non-contiguous ("fragmented") blocks** within the journal area.

Standard JBD2 treats the journal as a strict ring buffer. Under high-metadata workloads this leads to premature "out of journal space" errors even when free blocks exist scattered across the journal. FJM solves this by maintaining a per-block bitmap and a per-transaction index, enabling JBD2 to allocate any free journal block regardless of physical position.

The implementation is done as a **loadable kernel module** (`ext4_tracker`) — the stock `ext4` driver is compiled separately with FJM hooks, so the original kernel ext4 is never replaced.

---

## Artifact Directory Structure

```
linux/
├── linux-6.1.4/                      # Modified kernel source tree
│   ├── fs/ext4_tracker/               # ★ ext4_tracker module source
│   │   │                              #   (extract from artifact/ext4_tracker.tar.gz)
│   │   ├── fjmap.c                   # ★ FJM core implementation (~930 lines)
│   │   ├── fjmap.h                   # ★ FJM on-disk/in-memory structures
│   │   ├── ext4.h                    # ★ Modified: EXT4_MOUNT2_FJM flag, s_fjmap field
│   │   ├── ext4_jbd2.c               # ★ Modified: begin_txn / commit_txn hooks
│   │   ├── super.c                   # ★ Modified: mount option 'fjm', init/destroy
│   │   └── Makefile                  # ★ Modified: fjmap.o added to ext4-y
│   ├── include/linux/jbd2.h          # ★ Modified via First.patch
│   └── fs/jbd2/
│       ├── journal.c                 # ★ Modified via First.patch
│       └── transaction.c             # ★ Modified via Second.patch
│
├── artifact/
│   ├── ext4_tracker.tar.gz           # ★ ext4_tracker module source archive
│   ├── First.patch                   # ★ Patch: jbd2/journal.c + include/linux/jbd2.h
│   ├── Second.patch                  # ★ Patch: fs/jbd2/transaction.c
│   └── benchmark/
│       ├── fjm_full_benchmark.sh     # ★ Full 6-mode benchmark (single run)
│       ├── run_repeated_benchmark.sh # ★ Repeat benchmark N times, aggregate result
│       └── wal_workload.fio          # fio WAL workload (100MB, fsync per write)
│
└── README.md                         # This file
```

---

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
sudo apt-get install -y fio blktrace
```

| Tool | Purpose |
|------|---------|
| `fio` | WAL benchmark workload generator |
| `blktrace` / `blkparse` | Physical write byte measurement (WAF calculation) |

---

## Linux Kernel Compilation Instructions

> **Estimated time**: ~30–45 minutes (parallel build on 4 cores)

### Step 1 — Extract the source

```bash
cd ~/
tar -xf linux-6.1.4.tar.xz
cd linux-6.1.4
```

### Step 2 — Configure

Use your running kernel's config as a base, then build as a module:

```bash
cp /boot/config-$(uname -r) .config
make olddefconfig
```

### Step 3 — Build the full kernel + modules

```bash
make -j$(nproc)
```

### Step 4 — Install

```bash
sudo make modules_install
sudo make install
sudo reboot
```

After reboot, verify the correct kernel is running:

```bash
uname -r    # should show 6.1.4
uname -v    # should show your build timestamp
```

### Step 5 — Apply kernel patches and build the ext4_tracker module

First apply the two patches to modify the JBD2 core:

```bash
cd ~/linux-6.1.4
git apply ~/artifact/First.patch
git apply ~/artifact/Second.patch
```

Then extract the `ext4_tracker` module source and configure it:

```bash
# Extract ext4_tracker into the kernel tree
tar -xzf ~/artifact/ext4_tracker.tar.gz -C fs/
```

Next, open the kernel configuration menu and enable the module:

```bash
make menuconfig
```

Navigate to:
```
File systems  →
    Ext4 TRACKER filesystem  →  M
```

Select **`M`** (build as loadable module), then save and exit.

> **Human time**: ~2 minutes. Use arrow keys to navigate, `M` to select module, `<Save>` then `<Exit>`.

Finally, compile the module:

```bash
make M=fs/ext4_tracker modules
```

This produces `fs/ext4_tracker/ext4_tracker.ko`.

### Step 6 — Load the module

```bash
sudo insmod fs/ext4_tracker/ext4_tracker.ko
```

Verify it is registered:

```bash
cat /proc/filesystems | grep ext4
# Expected output:
#         ext4           ← built-in kernel ext4
#         ext4_tracker   ← your FJM module
```

---

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



## Assumptions and Unsupported Features

### Assumptions

- The journal size is fixed at format time (`mkfs.ext4 -J size=N`). FJM reads `j_first`/`j_last` from JBD2 at mount time and never changes the bitmap size afterwards.
- The test device (`/dev/sdb`) is dedicated. Scripts call `mkfs.ext4 -F` (force-format) without confirmation.
- The module is always loaded **before** mounting. Mounting as `ext4_tracker` without the module loaded will fail with `unknown filesystem type`.

### Unsupported Features

| Feature | Status | Notes |
|---------|--------|-------|
| Crash Recovery / Replay | ❌ Not implemented | On-disk index (Phase 2A) is written but the JBD2 recovery path does not yet read it. After a crash, the standard JBD2 replay runs instead. |

---

## Getting Started (≤ 30 Minutes)

This section lets you verify basic FJM functionality quickly.

### Prerequisites

- Kernel 6.1.4 is booted (`uname -r`)
- `ext4_tracker.ko` is built (`fs/ext4_tracker/ext4_tracker.ko` exists)
- `/dev/sdb` is available and can be wiped
- `fio` is installed (`fio --version`)

### Quick Functional Test (~5 minutes)

```bash
# 1. Load the FJM module
sudo insmod fs/ext4_tracker/ext4_tracker.ko

# 2. Verify registration
cat /proc/filesystems | grep ext4_tracker

# 3. Run the full benchmark
cd ~/linux-6.1.4
sudo bash ~/artifact/benchmark/fjm_full_benchmark.sh

# 4. Check dmesg for FJM activity
sudo dmesg | grep "FJM:"
```

**Expected dmesg output:**
```
EXT4-fs (sdb): FJM: initialised, tracking 4095 journal blocks (j_first=1 j_last=4096 index_blocks=16)
EXT4-fs (sdb): FJM: Allocated journal blk=42
EXT4-fs (sdb): FJM: Allocated journal blk=87
EXT4-fs (sdb): FJM: Freed 1013 blocks for checkpointed tid=2
...
```

**Expected benchmark result** (from `fjm_full_benchmark.sh`):
```
  RUN                  BW(KB/s)       IOPS    P99(ms)  Logical(MB)   Phys(MB)        WAF
  ordered_std              2773      346.7      1.139          100      250.0      2.50x
  ordered_fjm              2725      340.7      0.063          100      250.0      2.50x
  journal_std              2973      371.7      1.188          100      350.0      3.50x
  journal_fjm              2092      261.6      0.659          100      362.8      3.62x
  writeback_std            2632      329.1      0.060          100      250.0      2.50x
  writeback_fjm            2933      366.7      0.042          100      250.0      2.50x
```

---

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

**Sample output** (Run 1 from `full_benchmark1.log`):
```
  RUN                  BW(KB/s)       IOPS    P99(ms)  Logical(MB)   Phys(MB)        WAF
  ordered_std              2242      280.3      0.709          100      250.0      2.50x
  ordered_fjm              2437      304.7      0.189          100      250.0      2.50x
  journal_std              2506      313.3      0.766          100      350.0      3.50x
  journal_fjm              1997      249.7      1.204          100      361.7      3.61x
  writeback_std            1814      226.8      0.253          100      250.0      2.50x
  writeback_fjm            2297      287.2      0.171          100      250.0      2.50x
```

### Experiment 2 — Repeated Multi-Run Benchmark (Statistical Stability)

| Field | Details |
|-------|---------|
| **Purpose** | Confirm WAF stability across 5 independent runs; observe bandwidth/latency variance |
| **Script** | `run_repeated_benchmark.sh` |
| **How to run** | `sudo bash run_repeated_benchmark.sh 5` |
| **Estimated runtime** | ~3.5–5 hours (5 × ~45–60 min) |
| **Expected result** | WAF stable (2.50x / 3.50x / 3.59–3.62x). Bandwidth varies ±15% across runs (OS scheduling jitter) |
| **Actual result location** | Console (results table per run only). Full logs in `repeated_run_logs/<timestamp>/run_N.log` |

**Observed results across 5 runs** (from `result_repeated_runs.log`):

| Mode | WAF Run1 | WAF Run2 | WAF Run3 | WAF Run4 | WAF Run5 |
|------|----------|----------|----------|----------|----------|
| ordered_std | 2.50x | 2.50x | 2.50x | 2.50x | 2.50x |
| ordered_fjm | 2.50x | 2.50x | 2.50x | 2.50x | 2.50x |
| journal_std | 3.50x | 3.50x | 3.50x | 3.50x | 3.50x |
| journal_fjm | 3.62x | 3.62x | 3.59x | 3.62x | 3.59x |
| writeback_std | 2.50x | 2.50x | 2.50x | 2.50x | 2.50x |
| writeback_fjm | 2.50x | 2.50x | 2.50x | 2.50x | 2.50x |

WAF is perfectly stable across all 5 runs — confirming the implementation is deterministic at the I/O amplification level.

### Experiment 3 — Heavy FJM Workload (Functional Diversity Test)

| Field | Details |
|-------|---------|
| **Purpose** | Stress-test FJM under diverse I/O patterns: parallel metadata, sequential data, random access, mixed |
| **Script** | `test_fjm_workload.sh` |
| **How to run** | `sudo bash test_fjm_workload.sh` |
| **Estimated runtime** | ~10 minutes |
| **Expected result** | No filesystem errors. `dmesg` shows continuous `FJM: Allocated` and `FJM: Freed` messages throughout |
| **Actual result location** | Console output + `sudo dmesg \| grep "FJM:"` |

---

## Interpreting Results

| Metric | Definition |
|--------|-----------|
| **BW (KB/s)** | Write bandwidth — higher is better |
| **IOPS** | Write operations per second — higher is better |
| **P99 (ms)** | 99th percentile write latency — lower is better. Represents worst-case fsync commit time experienced by 1 in 100 writes |
| **Logical (MB)** | Bytes written by the application (always 100MB) |
| **Physical (MB)** | Bytes actually written to disk (measured by blktrace) — includes journal overhead |
| **WAF** | Write Amplification Factor = Physical / Logical. The journal overhead multiplier |

### WAF Interpretation by Mode

| Mode | Expected WAF | Reason |
|------|-------------|--------|
| ordered | 2.50x | Data written once + metadata journaled once |
| journal | 3.50x | Data written twice (journal + final location) + metadata |
| writeback | 2.50x | Same as ordered; metadata timing relaxed |
| journal + FJM | ~3.60x | Slightly higher than 3.50x due to FJM index block writes |

---

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
