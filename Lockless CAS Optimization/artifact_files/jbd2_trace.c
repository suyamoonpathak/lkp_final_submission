/*
 * jbd2_trace.c — Fsync and JBD2 latency analysis tracer & optimization
 * Usage:
 *   sudo insmod jbd2_trace.ko device=nvme0n1p7 optimize=0
 *   sudo insmod jbd2_trace.ko device=nvme0n1p7 optimize=1
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/kprobes.h>
#include <linux/ktime.h>
#include <linux/atomic.h>
#include <linux/sched.h>
#include <linux/string.h>
#include <linux/fs.h>
#include <linux/jbd2.h>

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("JBD2 fsync latency tracer and optimizer");

static char *device = "";
module_param(device, charp, 0444);

static int optimize = 0;
module_param(optimize, int, 0444);

/* ── helpers ── */
struct ts_data { u64 start; };

struct probe_cnt {
	const char  *name;
	const char  *symbol;
	const char  *section;
	const char  *lock_name;
	atomic64_t   count, total_ns, max_ns, min_ns;
	int          registered;
	struct kretprobe krp;
};

static atomic64_t bench_start = ATOMIC64_INIT(0);
static atomic64_t bench_end   = ATOMIC64_INIT(0);

static void do_max(atomic64_t *v, s64 n)
{ s64 o; do { o=atomic64_read(v); if(n<=o)return; } while(atomic64_cmpxchg(v,o,n)!=o); }
static void do_min(atomic64_t *v, s64 n)
{ s64 o; do { o=atomic64_read(v); if(o&&n>=o)return; } while(atomic64_cmpxchg(v,o,n)!=o); }

/* ── OPTIMIZATION LOGIC ── */
static atomic64_t opt_count    = ATOMIC64_INIT(0);
static atomic64_t opt_total_ns = ATOMIC64_INIT(0);
static atomic64_t opt_max_ns   = ATOMIC64_INIT(0);
static atomic64_t opt_min_ns   = ATOMIC64_INIT(0);

static atomic_t wakeup_cas = ATOMIC_INIT(0);

static int noinline optimized_log_wait_commit(journal_t *journal, tid_t tid)
{
	u64 start, dur;
	int err = 0;
	start = ktime_get_ns();

	if (!tid_gt(tid, READ_ONCE(journal->j_commit_sequence)))
		goto out;

	if (tid_gt(tid, READ_ONCE(journal->j_commit_request))) {
		if (atomic_cmpxchg(&wakeup_cas, 0, 1) == 0) {
			read_lock(&journal->j_state_lock);
			if (tid_gt(tid, journal->j_commit_request))
				wake_up(&journal->j_wait_commit);
			read_unlock(&journal->j_state_lock);
			atomic_set(&wakeup_cas, 0);
		}
	}

	wait_event(journal->j_wait_done_commit,
		   !tid_gt(tid, READ_ONCE(journal->j_commit_sequence)));
	smp_rmb();

out:
	if (unlikely(is_journal_aborted(journal)))
		err = -EIO;

	dur = ktime_get_ns() - start;
	atomic64_inc(&opt_count);
	atomic64_add(dur, &opt_total_ns);
	do_max(&opt_max_ns, dur);
	do_min(&opt_min_ns, dur);
	return err;
}

static struct kprobe waitcmt_kp;
static int __kprobes jbd2_lwc_handler(struct kprobe *p, struct pt_regs *regs)
{
	if (device[0]) {
		journal_t *j = (journal_t *)regs->di;
		if (j && strstr(j->j_devname, device) == NULL)
			return 0;
	}
	regs->ip = (unsigned long)optimized_log_wait_commit;
	return 1;
}

/* ── custom fsync tracker for accurate workload timing ── */
static struct probe_cnt p_fsync;
static int entry_fsync(struct kretprobe_instance *ri, struct pt_regs *regs) {
	u64 now;
	struct ts_data *d;
	if (device[0]) {
		struct file *f = (struct file *)regs->di;
		if (f && f->f_inode && f->f_inode->i_sb &&
		    strstr(f->f_inode->i_sb->s_id, device) == NULL)
			return 1;
	}
	now = ktime_get_ns();
	d = (struct ts_data *)ri->data;
	d->start = now;
	if (atomic64_read(&bench_start) == 0)
		atomic64_cmpxchg(&bench_start, 0, now);
	return 0;
}
static int p_fsync_ret(struct kretprobe_instance *ri, struct pt_regs *r) {
	struct ts_data *d = (struct ts_data *)ri->data;
	u64 now = ktime_get_ns();
	u64 dur = now - d->start;
	atomic64_inc(&p_fsync.count); atomic64_add(dur, &p_fsync.total_ns);
	do_max(&p_fsync.max_ns, dur); do_min(&p_fsync.min_ns, dur);
	do_max(&bench_end, now);
	return 0;
}
static struct probe_cnt p_fsync = {
	.name = "ext4_sync_file", .symbol = "ext4_sync_file", .section = "PATH",
	.krp = { .kp.symbol_name = "ext4_sync_file", .handler = p_fsync_ret,
	         .entry_handler = entry_fsync, .data_size = sizeof(struct ts_data), .maxactive = 64 }
};

/* ── generic dev filter handlers ── */
static int entry_journal(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct ts_data *d;
	if (device[0]) {
		journal_t *j = (journal_t *)regs->di;
		if (j && strstr(j->j_devname, device) == NULL) return 1;
	}
	d = (struct ts_data *)ri->data;
	d->start = ktime_get_ns();
	return 0;
}

static int entry_handle(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct ts_data *d;
	if (device[0]) {
		handle_t *h = (handle_t *)regs->di;
		if (h && h->h_transaction && h->h_transaction->t_journal &&
		    strstr(h->h_transaction->t_journal->j_devname, device) == NULL)
			return 1;
	}
	d = (struct ts_data *)ri->data;
	d->start = ktime_get_ns();
	return 0;
}

static int entry_file(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct ts_data *d;
	if (device[0]) {
		struct file *f = (struct file *)regs->di;
		if (f && f->f_inode && f->f_inode->i_sb &&
		    strstr(f->f_inode->i_sb->s_id, device) == NULL)
			return 1;
	}
	d = (struct ts_data *)ri->data;
	d->start = ktime_get_ns();
	return 0;
}

static int entry_comm(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct ts_data *d;
	if (device[0] && strncmp(current->comm, "jbd2/", 5) == 0 &&
	    strstr(current->comm + 5, device) != current->comm + 5)
		return 1;
	d = (struct ts_data *)ri->data;
	d->start = ktime_get_ns();
	return 0;
}

/* ── kretprobe macro ── */
#define DEFINE_PROBE(VAR, SYM, LABEL, SEC, LOCKNAME, MAXACT, ENTRY_FN)    \
static struct probe_cnt VAR;                                               \
static int VAR##_ret(struct kretprobe_instance *ri, struct pt_regs *r) {   \
	struct ts_data *d = (struct ts_data *)ri->data;                    \
	u64 dur = ktime_get_ns() - d->start;                              \
	atomic64_inc(&VAR.count); atomic64_add(dur, &VAR.total_ns);        \
	do_max(&VAR.max_ns, dur); do_min(&VAR.min_ns, dur);                \
	return 0;                                                          \
}                                                                          \
static struct probe_cnt VAR = {                                            \
	.name = LABEL, .symbol = SYM, .section = SEC,                      \
	.lock_name = LOCKNAME, .registered = 0,                            \
	.krp = { .kp.symbol_name = SYM, .handler = VAR##_ret,             \
	         .entry_handler = ENTRY_FN,                                \
	         .data_size = sizeof(struct ts_data),                       \
	         .maxactive = MAXACT }                                     \
}

/* ═══ FSYNC PATH probes ═══ */
DEFINE_PROBE(p_writewait, "file_write_and_wait_range",
	"file_write_and_wait_range","PATH", NULL, 64, entry_file);
DEFINE_PROBE(p_waitcmt,   "jbd2_log_wait_commit",
	"jbd2_log_wait_commit",     "PATH", NULL, 64, entry_journal);
DEFINE_PROBE(p_commit,    "jbd2_journal_commit_transaction",
	"jbd2_commit_transaction",  "PATH", NULL, 4,  entry_journal);
DEFINE_PROBE(p_flush,     "blkdev_issue_flush",
	"blkdev_issue_flush",       "PATH", NULL, 64, entry_comm);

/* ═══ LOCK probes ═══ */
DEFINE_PROBE(p_starthdl,  "start_this_handle",
	"start_this_handle",        "LOCK", "j_state_lock",  64, entry_journal);
DEFINE_PROBE(p_stop,      "jbd2_journal_stop",
	"jbd2_journal_stop",        "LOCK", "j_state_lock",  64, entry_handle);
DEFINE_PROBE(p_dirty,     "jbd2_journal_dirty_metadata",
	"jbd2_dirty_metadata",      "LOCK", "j_list_lock",   64, entry_handle);
DEFINE_PROBE(p_getwr,     "jbd2_journal_get_write_access",
	"jbd2_get_write_access",    "LOCK", "bh_state_lock", 64, entry_handle);

static struct probe_cnt *all[] = {
	&p_writewait, &p_waitcmt, &p_commit, &p_flush,
	&p_starthdl, &p_stop, &p_dirty, &p_getwr,
};
#define N ARRAY_SIZE(all)

static u64 trace_start_time;

/* ═══ Init ═══ */
static int __init jbd2_trace_init(void)
{
	int i, ok = 0, ret;

	if (optimize < 0 || optimize > 1) {
		pr_err("jbd2_trace: optimize must be 0 or 1\n");
		return -EINVAL;
	}

	trace_start_time = ktime_get_ns();

	pr_info("jbd2_trace: loading — device=%s  optimize=%d (%s)\n",
		device[0] ? device : "(all)", optimize,
		optimize ? "lockless wait" : "baseline");

	/* Custom init for p_fsync */
	ret = register_kretprobe(&p_fsync.krp);
	if (ret < 0) pr_warn("jbd2_trace: SKIP ext4_sync_file err=%d\n", ret);
	else p_fsync.registered = 1;

	/* Register all kretprobes */
	for (i = 0; i < N; i++) {
		if (optimize && all[i] == &p_waitcmt)
			continue;
		ret = register_kretprobe(&all[i]->krp);
		if (ret < 0)
			pr_warn("jbd2_trace: SKIP %-30s err=%d\n",
				all[i]->symbol, ret);
		else { all[i]->registered = 1; ok++; }
	}

	/* Register optimizer if enabled */
	if (optimize) {
		waitcmt_kp.symbol_name = "jbd2_log_wait_commit";
		waitcmt_kp.pre_handler = jbd2_lwc_handler;
		ret = register_kprobe(&waitcmt_kp);
		if (ret < 0) pr_err("jbd2_trace: optimize kprobe FAILED\n");
		else ok++;
	}

	if (!ok && !p_fsync.registered) return -ENOENT;
	pr_info("jbd2_trace: %d probes active. rmmod for report.\n", ok);
	return 0;
}

/* ═══ Exit — unified report ═══ */
static void __exit jbd2_trace_exit(void)
{
	int i;
	s64 fsync_ns, lock_total_ns = 0;

	if (p_fsync.registered)
		unregister_kretprobe(&p_fsync.krp);
	for (i = 0; i < N; i++)
		if (all[i]->registered) unregister_kretprobe(&all[i]->krp);
	if (optimize)
		unregister_kprobe(&waitcmt_kp);

	if (optimize) {
		atomic64_set(&p_waitcmt.count,    atomic64_read(&opt_count));
		atomic64_set(&p_waitcmt.total_ns, atomic64_read(&opt_total_ns));
		atomic64_set(&p_waitcmt.max_ns,   atomic64_read(&opt_max_ns));
		atomic64_set(&p_waitcmt.min_ns,   atomic64_read(&opt_min_ns));
	}

	fsync_ns = atomic64_read(&p_fsync.total_ns);

	pr_info("jbd2_trace: ============================================\n");
	pr_info("jbd2_trace:  FSYNC AND WAIT-COMMIT LATENCY  (device: %s)\n",
		device[0] ? device : "(all)");
	pr_info("jbd2_trace:  Mode: %s\n", optimize ? "LOCKLESS WAIT [SAFE]" : "BASELINE");
	pr_info("jbd2_trace: ============================================\n");
	pr_info("jbd2_trace: %-36s %8s %9s %8s %8s %8s\n",
		"PHASE", "COUNT", "TOTAL_ms", "AVG_us", "MIN_us", "MAX_us");

	if (atomic64_read(&p_fsync.count)) {
		s64 c = atomic64_read(&p_fsync.count);
		s64 t = atomic64_read(&p_fsync.total_ns);
		pr_info("jbd2_trace: %-36s %8lld %9lld %8lld %8lld %8lld\n",
			p_fsync.name, (long long)c, (long long)(t/1000000), (long long)((t/c)/1000), 
			(long long)(atomic64_read(&p_fsync.min_ns)/1000), (long long)(atomic64_read(&p_fsync.max_ns)/1000));
	}

	for (i = 0; i < N; i++) {
		s64 c, t;
		if (strcmp(all[i]->section, "PATH")) continue;
		c = atomic64_read(&all[i]->count);
		t = atomic64_read(&all[i]->total_ns);
		if (!c) continue;
		
		pr_info("jbd2_trace: %-36s %8lld %9lld %8lld %8lld %8lld\n",
			all[i]->name, (long long)c, (long long)(t/1000000), (long long)((t/c)/1000),
			(long long)(atomic64_read(&all[i]->min_ns)/1000), (long long)(atomic64_read(&all[i]->max_ns)/1000));
	}

	/* Aggregate lock time over all tracking lock hooks (hiding the count table) */
	for (i = 0; i < N; i++) {
		if (strcmp(all[i]->section, "LOCK")) continue;
		lock_total_ns += atomic64_read(&all[i]->total_ns);
	}

	{
		u64 bs = atomic64_read(&bench_start);
		u64 be = atomic64_read(&bench_end);
		u64 workload_real_ns = (bs && be && be > bs) ? (be - bs) : 0;
		s64 fc = atomic64_read(&p_fsync.count);

		pr_info("jbd2_trace:\n");
		pr_info("jbd2_trace: ── SUMMARY ──\n");
		pr_info("jbd2_trace: 1. Total real workload execution time: %lld ms\n", (long long)(workload_real_ns/1000000));
		pr_info("jbd2_trace: 2. Total aggregated fsync CPU time: %lld ms\n", (long long)(fsync_ns/1000000));
		/* Print total lock time without displaying the individual confusing lock counts */
		pr_info("jbd2_trace: 3. Total aggregated time taken on metadata locks: %lld ms\n", (long long)(lock_total_ns/1000000));
		pr_info("jbd2_trace: 4. Total count of fsyncs: %lld\n", (long long)fc);
	}

	for (i = 0; i < N; i++)
		if (all[i]->registered && all[i]->krp.nmissed > 0)
			pr_warn("jbd2_trace: MISSED %s: %d\n", all[i]->name, all[i]->krp.nmissed);

	pr_info("jbd2_trace: done.\n");
}

module_init(jbd2_trace_init);
module_exit(jbd2_trace_exit);
