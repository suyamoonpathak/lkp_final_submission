import json
import os
import matplotlib.pyplot as plt
import numpy as np

# Bind dynamically mapped to current directry
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
RES_DIR = os.path.join(MODULE_DIR, "benchmark_results")

MODES = ["ordered", "writeback"]
OPTS = [0, 1]
THREADS = [1, 4, 8, 16]
RUNS = [1, 2, 3]

def parse_fio_json(filepath):
    try:
        if not os.path.exists(filepath): return 0.0
        with open(filepath) as f:
            raw = f.read()
        idx = raw.find('{')
        if idx == -1: return 0.0
        data = json.loads(raw[idx:])
        
        if 'jobs' in data and len(data['jobs']) > 0 and 'write' in data['jobs'][0]:
            return float(data['jobs'][0]['write']['iops'])
        return 0.0
    except Exception as e:
        return 0.0

def parse_sysbench(filepath):
    try:
        if not os.path.exists(filepath): return 0.0
        with open(filepath) as f:
            for line in f:
                if "writes/s:" in line:
                    return float(line.split()[1])
    except Exception:
        return 0.0

def parse_fsmark(filepath):
    try:
        if not os.path.exists(filepath): return 0.0
        with open(filepath) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
                    return float(parts[3])
    except Exception:
        pass
    return 0.0

def parse_dbench(filepath):
    try:
        if not os.path.exists(filepath): return 0.0
        with open(filepath) as f:
            for line in f:
                if "Throughput" in line and "MB/sec" in line:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p == "Throughput":
                            return float(parts[i+1])
    except Exception:
        pass
    return 0.0

results = {
    'fio': {}, 'sysbench': {}, 'fsmark': {}, 'dbench': {}
}

for m in MODES:
    for o in OPTS:
        key = f"{m}_opt{o}"
        results['fio'][key] = []
        results['sysbench'][key] = []
        results['fsmark'][key] = []
        results['dbench'][key] = []
        
        for t in THREADS:
            fio_avg, sys_avg, fs_avg, db_avg = [], [], [], []
            
            for r in RUNS:
                pref = f"{m}_opt{o}_t{t}_r{r}"
                fio_avg.append(parse_fio_json(os.path.join(RES_DIR, f"{pref}_fio.json")))
                sys_avg.append(parse_sysbench(os.path.join(RES_DIR, f"{pref}_sysbench.txt")))
                fs_avg.append(parse_fsmark(os.path.join(RES_DIR, f"{pref}_fsmark.txt")))
                db_avg.append(parse_dbench(os.path.join(RES_DIR, f"{pref}_dbench.txt")))
            
            results['fio'][key].append(np.mean(fio_avg) if fio_avg else 0.0)
            results['sysbench'][key].append(np.mean(sys_avg) if sys_avg else 0.0)
            results['fsmark'][key].append(np.mean(fs_avg) if fs_avg else 0.0)
            results['dbench'][key].append(np.mean(db_avg) if db_avg else 0.0)

fig, axes = plt.subplots(2, 2, figsize=(16, 12))
fig.suptitle('JBD2 Lockless Optimization Performance (Averaged over 3 runs)', fontsize=18, fontweight='bold', y=0.98)

def plot_ax(ax, bench_key, title, y_label):
    x = np.arange(len(THREADS))
    width = 0.2
    colors = ['#e74c3c', '#2ecc71', '#c0392b', '#27ae60']
    keys = ['ordered_opt0', 'ordered_opt1', 'writeback_opt0', 'writeback_opt1']
    labels = ['Ordered (Baseline, opt=0)', 'Ordered (Optimized, opt=1)',
              'Writeback (Baseline, opt=0)', 'Writeback (Optimized, opt=1)']
              
    for i, k in enumerate(keys):
        ax.bar(x + (i - 1.5) * width, results[bench_key][k], width, color=colors[i], label=labels[i])
        
    ax.set_title(title, fontsize=14, pad=10)
    ax.set_xlabel('Num Threads (Concurrency)', fontsize=12)
    ax.set_ylabel(f"Units: {y_label}", fontsize=12, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(THREADS)
    ax.grid(True, axis='y', linestyle='--', alpha=0.5)
    ax.legend(title='Journal Mode & Opt state', fontsize=10)

plot_ax(axes[0,0], 'fio', 'FIO (Contention Stress)', 'Write IOPS (Operations/sec, Higher = Better)')
plot_ax(axes[0,1], 'sysbench', 'Sysbench (Database)', 'Sync IOPS (Operations/sec, Higher = Better)')
plot_ax(axes[1,0], 'fsmark', 'fs_mark (Metadata)', 'Throughput (Files/sec, Higher = Better)')
plot_ax(axes[1,1], 'dbench', 'dbench (Streaming)', 'Throughput (Megabytes/sec, Higher = Better)')

plt.tight_layout(rect=[0, 0.03, 1, 0.95])
plot_path = os.path.join(MODULE_DIR, "optimization_results.png")
plt.savefig(plot_path, dpi=300)
print(f"Successfully generated 3-run dynamically averaged bar charts at: {plot_path}")
