#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#include <linux/types.h>

extern int vli_cmp(const u64 *left, const u64 *right, unsigned int ndigits);

static const u64 V[8][4] = {
	{0,0,0,0}, {1,0,0,0}, {0,0,0,1}, {0xffffffffffffffffULL,0,0,0},
	{0,1,0,0}, {0xdeadbeef,0xcafe,0,0}, {0xffffffffffffffffULL,0xffffffffffffffffULL,0xffffffffffffffffULL,0xffffffffffffffffULL},
	{5,5,5,5},
};

static int __init cgir_vli_probe_init(void)
{
	u64 digest = 0x9e3779b97f4a7c15ULL;
	int i, j, n;

	for (i = 0; i < 8; i++)
		for (j = 0; j < 8; j++)
			for (n = 1; n <= 4; n++) {
				int r = vli_cmp(V[i], V[j], n);
				digest ^= (u64)(u32)r + (digest << 6) + (digest >> 2) + (u64)(i*64 + j*4 + n);
			}
	pr_info("CGIR_PROBE vli_cmp digest=%016llx\n", digest);
	return 0;
}
late_initcall(cgir_vli_probe_init);
