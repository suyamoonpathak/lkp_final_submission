# Shrey Sharma (251110068) — Fragmented Journal Map (FJM) for ext4

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
shrey/
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

## Reproduce

> **Hardware needed:** a dedicated test block device (default
> `/dev/sdb`, 2 GB or more). The script formats it with
> `mkfs.ext4 -F` before every run, so do **not** point at a
> partition with real data.
>
> **Software needed:** `fio`, `blktrace`/`blkparse`,
> Linux 6.1.4 source tree (for the in-tree patches and module
> build).

```bash
# 1. Apply the two JBD2 patches to your linux-6.1.4 source tree
cd /path/to/linux-6.1.4
git apply /path/to/MTech\ Rocks/shrey/patches/First.patch
git apply /path/to/MTech\ Rocks/shrey/patches/Second.patch

# 2. Extract the module source into fs/
tar xzf /path/to/MTech\ Rocks/shrey/module/ext4_tracker.tar.gz -C fs/

# 3. Enable the module in menuconfig:
#    File systems -> "Ext4 TRACKER filesystem" -> M
make menuconfig

# 4. Build the kernel and the module
cp /boot/config-$(uname -r) .config
make olddefconfig
make -j$(nproc)
sudo make modules_install
sudo make install
sudo reboot     # boot into the rebuilt 6.1.4

# After reboot:
sudo insmod fs/ext4_tracker/ext4_tracker.ko
cat /proc/filesystems | grep ext4_tracker   # should list ext4_tracker

# 5. Run the benchmark suite
cd /path/to/MTech\ Rocks/shrey/benchmarks
sudo bash fjm_full_benchmark.sh             # ~40-60 minutes, 6 modes
# or for stability:
sudo bash run_repeated_benchmark.sh 5       # ~3.5-5 hours
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

## Notes for evaluators

- **Crash recovery is not implemented yet.** The on-disk FJM index
  is written but the JBD2 recovery path does not yet consume it,
  so after a crash the standard JBD2 replay runs instead. This is
  documented as a known limitation by the author and is the
  primary item of future work.
- The module is loaded with `insmod`. Mounting as `ext4_tracker`
  before the module is loaded fails with `unknown filesystem
  type`.
- The script wipes `/dev/sdb` with `mkfs.ext4 -F` on every run.
  Re-point the `DEV` variable at the top of the script for a
  different device.
