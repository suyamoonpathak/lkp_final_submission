# Lockless CAS for `jbd2_log_wait_commit`

This repository implements a lightweight, lockless double-checked Compare-And-Swap (CAS) optimization designed to natively bypass heavy `j_state_lock` CPU contention bottlenecks deep within the Linux Ext4 Journaling layer.

## Artifact Folder Structure

```text
Lockless CAS Optimization/
├── README.md                        ← This execution overview
└── artifact_files/
    ├── jbd2_trace.c                 ← Target kernel module with the custom CAS proxy logic
    ├── Makefile                     ← Out-of-tree runtime compilation hooks
    ├── dmesg_sampler.sh             ← Generates empirical kernel lock-latency tracking traces 
    ├── validation.sh                ← Actively hammers Ext4 structural crash-consistency boundaries
    ├── run_evals.sh                 ← Executable multi-threaded scaling arrays (FIO, Sysbench, fs_mark, dbench)
    ├── master_runner.sh             ← Automated suite script
    ├── plot_results.py              ← Reads array logs dynamically to visually render clustered bar-charts
    ├── benchmark_results/           ← Output directory where your raw data logs are securely saved
    └── optimization_results.png     ← Final generated metric graphics chart output
```

## Reproduction Sequence

The complex benchmark sweeps, filesystem safety-checks, and scientific graphing utilities have been intentionally separated so you can functionally step through the evaluation natively.

> **Target Environment:** Standard Linux Kernel natively deployed with Linux header files installed natively. Our execution module requires absolutely zero in-tree modification or internal OS patching to test.

### Step 1: Topology Preparation
Before launching natively, strictly identify your physical NVMe drive boundary and target directory mount point limits explicitly. 
*Example inputs:* Target Device = `/dev/nvme0n1p6`, Mount Path = `/mnt/analysis_2`.

### Step 2: Native Compilation
Open your standard bash terminal and navigate inside our bundled artifact footprint.
```bash
cd "artifact_files/"
make clean && make
```
*(Safely verify that the `jbd2_trace.ko` kernel object successfully compiled).*

### Step 3: Ext4 Crash-Consistency Validation
First, explicitly prove that the custom CAS-gate functionally preserves standard Ext4 crash-safety metrics by natively stressing it heavily prior to full scaling matrices.

```bash
chmod +x validation.sh
sudo ./validation.sh
```
- Input your Device and Mount points when dynamically prompted.

### Step 4: Kernel Latency Tracing
Extract exactly how much CPU latency is bottlenecked exclusively by the core internal `j_state_lock` directly from the Linux ring buffer logically.

```bash
chmod +x dmesg_sampler.sh
sudo ./dmesg_sampler.sh
```
- The native scripts securely drop the tracked execution arrays entirely back into your `dmesg` buffer identically.

### Step 5: Full Scalability Benchmark Matrix
Trigger the multi-threaded benchmark throughput arrays organically (scaling securely from 1 up to 16 native CPU threads dynamically), handling OS cache boundaries identically.

```bash
chmod +x run_evals.sh
sudo ./run_evals.sh
```
- Please securely leave your bash terminal alone to comfortably execute natively. **It might take around ~30 mins.**

### Step 6: Python Graphic Rendering
Use the python script to parse the output logs and generate the plot graphs.

```bash
python3 plot_results.py
```
- Our native script dynamically reads all generated `.json` and `.txt` metrics physically saved within the `benchmark_results/` container logically, and parses them cleanly into `optimization_results.png` directly inside your folder!
