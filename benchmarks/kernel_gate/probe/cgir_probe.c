// CGIR rung-4 gate probe — IDENTICAL in both legs. A late_initcall (runs
// during kernel_init_freeable, before the rootfs mount that panics a
// no-initramfs boot) calls the target on a fixed vector grid and printks a
// single deterministic digest line. The whole-program gate compares that line
// stock-vs-Rust: a rewrite that changes behavior changes the digest.
//
// The target is declared extern here and DEFINED elsewhere (cgir_target.c in
// the stock leg, a Rust object in the rewrite leg) — so this file never has to
// change between legs.
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/printk.h>

extern u64 cgir_target(u64 x);

static int __init cgir_probe_init(void)
{
	u64 digest = 0x9e3779b97f4a7c15ULL;
	int i;

	for (i = 0; i < 64; i++) {
		u64 in = (u64)i * 0x0123456789abcdefULL + (u64)(i ^ 0x5a);
		u64 out = cgir_target(in);
		digest ^= out + (digest << 6) + (digest >> 2) + (u64)i;
	}
	pr_info("CGIR_PROBE cgir_target digest=%016llx\n", digest);
	return 0;
}
late_initcall(cgir_probe_init);
