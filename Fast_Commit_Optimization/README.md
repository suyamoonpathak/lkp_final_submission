# FastCommit Coverage Extensions

This folder holds the complete artifact for two ext4 FastCommit
extensions that close coverage gaps in the upstream Linux 6.1.4
implementation:

- **C3 — Inline xattr fast commit.** Removes the
  `EXT4_FC_REASON_XATTR` fallback when an extended-attribute write
  fits inside the inode's inline area, by emitting a normal inode
  fast-commit tag instead of forcing a full transaction commit.
- **C4 — Fallocate range fast commit.** Removes the
  `EXT4_FC_REASON_FALLOC_RANGE` fallback for `COLLAPSE_RANGE` and
  `INSERT_RANGE`, by emitting an `ADD_RANGE` fast-commit tag over the
  affected logical blocks. Replay safety is guaranteed by the
  pre-existing `ext4_fc_set_bitmaps_and_counters` reconciliation step.

Two earlier candidates (C1 and C2) produced null results and are
preserved here as postmortems for transparency.

## Layout

```
Fast_Commit_Optimization/
├── README.md                            ← this file
├── fc-inline-xattr.patch                ← C3 patch (apply -p1 in linux-6.1.4)
├── fc-fallocate-range.patch             ← C4 patch
│
├── INSTRUCTIONS_BAREMETAL_C3.md         ← step-by-step bare-metal eval for C3
├── INSTRUCTIONS_BAREMETAL_C4.md         ← step-by-step bare-metal eval for C4
├── eval_baremetal_c3.sh                 ← bench orchestrator for C3
├── eval_baremetal_c4.sh                 ← bench orchestrator for C4
│
├── bench_xattr.sh                       ← C3 microbench (5000 setxattr+fsync, default N=5000)
├── xattr_fsync_helper.c                 ← per-op fsync helper for C3 bench
├── bench_fallocate_range.sh             ← C4 microbench
├── fallocate_range_helper.c             ← per-op fsync helper for C4 bench
│
├── c3_crash_test_a.sh                   ← C3 crash test: 100 inline xattrs survive crash
├── c3_crash_test_b.sh                   ← C3 crash test: set 100, remove 50, exactly 50 remain
├── c3_crash_test_c.sh                   ← C3 crash test: 668-byte block xattr (forced block path)
├── c4_crash_test_a.sh                   ← C4 crash test: collapse range survives crash
├── c4_crash_test_b.sh                   ← C4 crash test: insert range survives crash
├── c4_crash_test_c.sh                   ← C4 crash test: interleaved collapse/insert + md5 check
│
├── eval_results_c3/                     ← C3 raw bench output (VM + bare-metal, stock + patched)
├── eval_results_c4/                     ← C4 raw bench output
├── xfstests_c3/                         ← C3 xfstests output (patched kernel only; generic/473 also fails on stock 6.1.4)
├── xfstests_c4/                         ← C4 xfstests output (patched kernel only; same generic/473 pre-existing failure)
├── CANDIDATE3_results.md                ← C3 writeup with all numbers
├── CANDIDATE4_results.md                ← C4 writeup with all numbers
│
├── characterization_results.md          ← measure-first study used to pick C4
├── bench_concurrent_fsync.sh            ← characterization bench: concurrent fsync
├── bench_xattr_block.sh                 ← characterization bench: xattr block path
├── concurrent_fsync_helper.c
├── xattr_block_fsync_helper.c
├── build_char_helpers.sh                ← compiles all four C helpers (xattr_fsync, fallocate_range, xattr_block, concurrent_fsync)
├── run_c4_characterization.sh
├── char_results/                        ← raw output of characterization runs
├── compare_results.py
│
├── jbd2-fc-barrier-defer.patch          ← C1 candidate (null result, preserved)
├── CANDIDATE1_postmortem.md
├── CANDIDATE1_README.md                 ← original C1 README (kept as historical doc)
├── CANDIDATE1_superseded_results.md     ← original C1 results, marked superseded
├── mballoc-async-prefetch.patch         ← C2 candidate (null result, preserved)
├── CANDIDATE2_postmortem.md
├── eval_results_c2/                     ← C2 raw bench output (perf.data dropped to keep ZIP small)
│
├── bench_async_prefetch.sh              ← C2 microbench
└── bench_fio_throughput.sh              ← regression check for C3
```

## Reproduction (from the `MTech Rocks/` root)

### Apply patches

```bash
cd /path/to/linux-6.1.4
patch -p1 < /path/to/MTech\ Rocks/Fast_Commit_Optimization/fc-inline-xattr.patch
patch -p1 < /path/to/MTech\ Rocks/Fast_Commit_Optimization/fc-fallocate-range.patch
# build kernel as in INSTRUCTIONS_BAREMETAL_C3.md, boot, then proceed
```

### Run C3 microbench

```bash
cd /path/to/MTech\ Rocks
bash Fast_Commit_Optimization/build_char_helpers.sh
sudo bash Fast_Commit_Optimization/eval_baremetal_c3.sh
# results land in Fast_Commit_Optimization/eval_results_c3/baremetal/
```

### Run C4 microbench

```bash
sudo bash Fast_Commit_Optimization/eval_baremetal_c4.sh
# results land in Fast_Commit_Optimization/eval_results_c4/baremetal/
```

### Run crash tests

```bash
sudo bash Fast_Commit_Optimization/c3_crash_test_a.sh
sudo bash Fast_Commit_Optimization/c3_crash_test_b.sh
sudo bash Fast_Commit_Optimization/c3_crash_test_c.sh
sudo bash Fast_Commit_Optimization/c4_crash_test_a.sh
sudo bash Fast_Commit_Optimization/c4_crash_test_b.sh
sudo bash Fast_Commit_Optimization/c4_crash_test_c.sh
# each prints PASS / FAIL and the recovered xattr / file contents
```

### Headline numbers (bare metal, full detail in CANDIDATE3/4_results.md)

| Workload | Stock | Patched | Wall-time | TX reduction |
|---|---|---|---|---|
| C3: 1000 inline-xattr setxattr+fsync | 57,627 ms / 5000 tx | 30,089 ms / 78 tx | -47.8% | 64× |
| C4: 1000 fallocate COLLAPSE+fsync | 12,445 ms / 1001 tx | 7,032 ms / 16 tx | -43.5% | 62.5× |

xfstests: 5 of 6 runnable tests pass on both C3 and C4 patched kernels;
the one failure (`generic/473`) reproduces identically on stock 6.1.4
and is therefore not a regression.

## Why C1 and C2 are kept

Their postmortems document why the optimization didn't land
(`fast_commit` not enabled in the bench rig for C1; <1% hit rate for
the optimized path in C2). They informed the measure-first
methodology that selected C4. The full report (`project_report.pdf`
at the top level) walks through each candidate.
