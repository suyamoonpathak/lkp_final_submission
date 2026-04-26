# MTech Rocks — CS614 Final Project Submission

This is the combined artifact submission for the **MTech Rocks**
group, CS614 Linux Kernel Programming, IIT Kanpur. The project
topic is *Optimizing the ext4/JBD2 consistency mechanism on Linux
6.1.4*. The four group members each pursued an independent
investigation; their work is in per-member subdirectories below,
and the unified writeup is `project_report.pdf`.

## Group

| Member            | Roll       | Contribution                                                          |
|-------------------|------------|------------------------------------------------------------------------|
| Milan Roy         | 241110042  | Workload characterization, JBD2 Study |
| Sahil Basia       | 241110061  | Lockless CAS implemented for locks in  `jbd2_log_wait_commit`|
| Shrey Sharma      | 251110068  | Fragmented Journal Map (FJM): JBD2 hooks + out-of-tree allocator module |
| Suyamoon Pathak   | 241110091  | Two FastCommit coverage extensions (inline xattr + fallocate range)    |

Contribution percentages are in `declaration.txt`.

## Artifact directory structure

```
MTech Rocks/
├── README.md                              ← this file
├── declaration.txt                        ← group declaration + per-member contribution %
├── Project_Report.pdf                     ← project report (covers all four members' work)
├── project_report.tex                     ← LaTeX source of the project report
│
├── Fast_Commit_Optimization/
│   └── README.md                          ← C3 + C4 fast-commit extensions, patches, benches, results
│
├── Lockless_CAS_Optimization/
│   └── README.md                          ← Lockless CAS module, benchmark script, journalling validation
│
├── Workload_Characteristics/
│   └── README.md                          ← per-mode JBD2 characterization, fio + bpftrace
│
└── Fragmeted_Jounral_Map_Optimization/
    └── README.md                          ← FJM module + JBD2 patches + WAL benchmark suite
```

Each per-member folder has its own `README.md` that lists what is
inside and how to reproduce that member's results. Start there for
member-specific instructions; this file is the top-level roadmap.

## Setup instructions

### Hardware

| Resource  | Minimum         | Recommended for full evaluation         |
|-----------|-----------------|------------------------------------------|
| CPU       | 2 cores         | 8+ cores |
| Memory    | 4 GB            | 8 GB                                                     |
| Storage   | 40 GB free      | An NVMe partition|
| Extra HW  | None            | None                                     |

A single-partition install is fine for Fast_Commit_Optimization's loop-image
benches. Lockless_CAS_Optimization's `run_evals.sh` and Workload_Characteristics'
`run_*_analysis.sh` expect a real ext4-formattable block device (default
`/dev/nvme0n1p6` or `/dev/nvme0n1p7`).

### Operating system

Ubuntu 22.04 LTS or 24.04 LTS, on a VM or bare-metal. We tested on
Ubuntu 24.04.2 inside VirtualBox (Fast_Commit_Optimization C3/C4 VM data) and on a
bare-metal Ubuntu host with an NVMe SSD (Fast_Commit_Optimization C3/C4 bare-metal
data, Lockless_CAS_Optimization matrix, Workload_Characteristics characterization).

The kernel version used is **6.1.4**. Before building, ensure the following config
flags are enabled (required for BTF metadata, kprobes, and debug symbols):

```bash
CONFIG_DEBUG_INFO
CONFIG_DEBUG_INFO_BTF
CONFIG_KPROBE_EVENTS
CONFIG_DEBUG_INFO_DWARF5
```

### Linux kernel build

Fast_Commit_Optimization's two patches (C3 and C4) need to be applied to a clean
Linux 6.1.4 source tree:

```bash
wget https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.1.4.tar.xz
tar xf linux-6.1.4.tar.xz
cd linux-6.1.4

# Apply C3 + C4 (both in Fast_Commit_Optimization/). They touch disjoint files and compose.
patch -p1 < /path/to/MTech\ Rocks/Fast_Commit_Optimization/fc-inline-xattr.patch
patch -p1 < /path/to/MTech\ Rocks/Fast_Commit_Optimization/fc-fallocate-range.patch

# Start from running-kernel config; set a distinct LOCALVERSION
cp /boot/config-$(uname -r) .config
sed -i 's/^CONFIG_LOCALVERSION=.*/CONFIG_LOCALVERSION="-mtechrocks"/' .config
make olddefconfig

make -j$(nproc) bzImage modules        # 20-45 min depending on cores

sudo make modules_install
sudo make install
sudo update-grub
sudo reboot                            # select 6.1.4-mtechrocks in GRUB
```

Lockless_CAS_Optimization module is built **out-of-tree against any installed Linux
6.1.4** with kernel headers — no in-tree patch required:

```bash
sudo apt install -y linux-headers-$(uname -r)
cd "MTech Rocks/Lockless_CAS_Optimization/artifact_files"
make clean && make                     # produces jbd2_trace.ko
```


### Software dependencies

Install once:

```bash
sudo apt update
sudo apt install -y \
  fio attr build-essential libncurses-dev bison flex libssl-dev \
  libelf-dev bc dwarves zstd patch wget gawk \
  linux-tools-common linux-tools-$(uname -r) \
  bpftrace sysbench fsmark dbench \
  python3-numpy python3-matplotlib
```

For xfstests (used by Fast_Commit_Optimization correctness evaluation):

```bash
sudo apt install -y xfslibs-dev libattr1-dev libacl1-dev libaio-dev \
    acl libtool-bin e2fsprogs libcap-dev quota uuid-runtime xfsprogs \
    autoconf-archive libtool automake liburing-dev pkg-config \
    libgdbm-dev
```

## Features supported

| # | Feature | Member | Where | Test scenarios | Expected outcome |
|---|---------|--------|-------|----------------|------------------|
| 1 | Fast Commit support for inline xattrs (Candidate 3) | Suyamoon | `Fast_Commit_Optimization/fc-inline-xattr.patch` | `bench_xattr.sh` (5000 setxattr+fsync), 3 crash tests | 64× reduction in full JBD2 commits, 27% wall-time speedup on VM, 48% on bare-metal |
| 2 | Fast Commit support for `fallocate(COLLAPSE\|INSERT_RANGE)` (Candidate 4) | Suyamoon | `Fast_Commit_Optimization/fc-fallocate-range.patch` | `bench_fallocate_range.sh`, 3 crash tests | 62.5× full-commit reduction, 23-26% wall-time gain on VM, 42-44% on bare-metal |
| 3 | Lockless CAS replacement of `jbd2_log_wait_commit` (Candidate 5) | Sahil | `Lockless_CAS_Optimization/module/jbd2_trace.ko` (load with `optimize=1`) | `run_evals.sh` matrix (4 workloads × 4 thread counts × 2 modes × 3 runs), `validation.sh` for validating journalling consistency | Output logs will be created by each benchmark, use python script to generate the plots |
| 4 | JBD2 lock-overhead measurement | Sahil | `Lockless_CAS_Optimization/module/jbd2_trace.ko` (load with `optimize=0`) | `dmesg_sampler.sh` | Output logs will be printed in dmesg |
| 5 | Per-mode ext4 journaling cost characterization | Milan | `Workload_Characteristics/Sync Heavy Workload/`, `Workload_Characteristics/Seq Write Workload/`, `Workload_Characteristics/Combined Workload/` | Sync-heavy fio at multiple block sizes (4k–64k); sequential write at 128k–1m; concurrent sync-heavy + sequential write; all run N times and averaged | `fsync` time dominates in Sync-heavy workload; Sync-heavy fio suffers in Combined Workload |
| 6 | Fragmented Journal Map (Candidate 6): replace JBD2 ring-buffer journal allocator with a free-block bitmap | Shrey | `Fragmeted_Jounral_Map_Optimization/patches/{First,Second}.patch` + `Fragmeted_Jounral_Map_Optimization/module/ext4_tracker.tar.gz` | `fjm_full_benchmark.sh` (6 modes), `run_repeated_benchmark.sh 5` (5-run stability) | `ordered_fjm` cuts P99 latency from 0.41 ms to 0.05 ms (8×) at same WAF; `journal_fjm` adds 3.50× → 3.61× WAF (index block journaled); WAF stable across 5 runs |

### Findings during evaluation

- No crashes, deadlocks, or assertion failures were observed during
  any member's evaluation of the patched kernel.
- `xfstests generic/473` fails identically on stock 6.1.4 and on
  every patched kernel we tried (C3, C3+C4; not run with
  Lockless_CAS_Optimization and Fragmented Jounral Map modules). It is a pre-existing failure, not a regression.
- C3 and C4 patches compose cleanly: with both applied,
  C3's xattr benefit is preserved on the C3+C4 kernel.

### Assumptions and unsupported features

Covered:
- Inline xattrs on ext4 (values that fit in the inode's extra
  region) — C3.
- SELinux labels, POSIX ACLs, short `user.*` xattrs — C3.
- Both `setxattr` and `removexattr` — C3.
- `fallocate(FALLOC_FL_COLLAPSE_RANGE)` and
  `fallocate(FALLOC_FL_INSERT_RANGE)` — C4.
- All ext4 journaling modes (`data=ordered`, etc.) for C3, C4, and
  C5; Lockless_CAS_Optimization also exercises `data=writeback`.

Not covered (left on the full-commit fallback by design, with the
reasoning in the report):
- Xattrs in a separate 4 KB block (large values) — C3 still falls
  back here; correct, just no speedup.
- `ea_inode` xattrs — C3 still falls back.
- `FALLOC_FL_PUNCH_HOLE`, ordinary `fallocate` — already handled
  upstream.
- `CROSS_RENAME`, `RENAME_DIR` — replay cannot yet safely update
  `..` dirents.
- `data=journal` mode for Lockless_CAS_Optimization— that mode
  saturates the SSD before any CPU lock contention manifests, so
  the optimization is invisible there. Intentionally not measured.

## Getting started (within 30 minutes)

This walkthrough exercises the C3 + C4 patches without rebuilding
the kernel, plus a build of Lockless_CAS_Optimization module.

1. **Verify Fast_Commit_Optimization patches apply cleanly.**

   ```bash
   wget https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.1.4.tar.xz
   tar xf linux-6.1.4.tar.xz
   cd linux-6.1.4
   patch -p1 --dry-run < ../"MTech Rocks/Fast_Commit_Optimization/fc-inline-xattr.patch"
   patch -p1 --dry-run < ../"MTech Rocks/Fast_Commit_Optimization/fc-fallocate-range.patch"
   ```

   Expected: `checking file ...` lines, no FAILED hunks. ~1 min.

2. **Compile-check the patched files in isolation.**

   ```bash
   patch -p1 < ../"MTech Rocks/Fast_Commit_Optimization/fc-inline-xattr.patch"
   patch -p1 < ../"MTech Rocks/Fast_Commit_Optimization/fc-fallocate-range.patch"
   cp /boot/config-$(uname -r) .config 2>/dev/null || make defconfig
   make olddefconfig
   make fs/ext4/xattr.o fs/ext4/fast_commit.o fs/ext4/extents.o
   ```

   Expected: 3 `CC` lines, no `error:`. ~1-2 min on 4 cores.

3. **Build Lockless_CAS_Optimization module against your running kernel.**

   ```bash
   cd "MTech Rocks/Lockless_CAS_Optimization/artifact_files"
   sudo apt install -y linux-headers-$(uname -r)
   make clean && make
   ```

   Expected: `jbd2_trace.ko` produced. ~30 s.

4. **Inspect recorded benchmark data.** All four members ship raw
   numbers, so you can verify the report's claims without running
   anything:

   ```bash
   # C3 + C4 (Fast_Commit_Optimization)
   cat "MTech Rocks/Fast_Commit_Optimization/eval_results_c3/baremetal/PATCHED_6.1.4-C3Patch/xattr_loop.txt"
   cat "MTech Rocks/Fast_Commit_Optimization/eval_results_c4/baremetal/PATCHED_C4_6.1.4-C3C4Patch/collapse_summary.txt"

   # Lockless_CAS_Optimization
   ls "MTech Rocks/Lockless_CAS_Optimization/artifact_files/benchmark_results/" | head
   cat "MTech Rocks/Lockless_CAS_Optimization/artifact_files/artifact_ans/dmesg_logs/"*.txt | head -30

   
   ```

5. **Read the per-member detail.** Each subfolder's `README.md` has
   the member-specific reproduction steps and headline numbers.

### Supplying your own inputs

- Fast_Commit_Optimization `bench_xattr.sh`: `N` (default 5000), `IMG_SIZE_MB`.
  Value length set inside `xattr_fsync_helper.c`; >80 bytes spills
  out of inline.
- Fast_Commit_Optimization `bench_fallocate_range.sh`: iterates over both modes,
  `N` (default 1000). Step size set in
  `fallocate_range_helper.c`.
- Lockless_CAS_Optimization `run_evals.sh`: prompts for `DEVICE` and `MOUNT_POINT`;
  edit thread sweep at the bottom of the script.
- Workload_Characteristics scripts: prompts for `DEVICE`, `MOUNT_POINT`, `SIZE`, `BLOCK_SIZE`

## Detailed evaluation

The full per-experiment table (purpose, run command, runtime,
expected result, where the output lands) is in the **Artifact
Evaluation appendix of `project_report.pdf`** (last section of the
report). It covers 12 experiments end-to-end across the four
contributions.

## Submission contents (this ZIP)

- `README.md` — this file
- `declaration.txt` — group declaration
- `project_report.pdf` — project report
- `Fast_Commit_Optimization/`, `Lockless_CAS_Optimization/`, `Workload_Characteristics/`, `Fragmeted_Jounral_Map_Optimization/` — per-member artifacts
- `combined_report/project_report.tex` — LaTeX source of the report

The Linux 6.1.4 kernel source tree is **not** in the ZIP (per
artifact rubric — patches only). Download it from
<https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.1.4.tar.xz>.
