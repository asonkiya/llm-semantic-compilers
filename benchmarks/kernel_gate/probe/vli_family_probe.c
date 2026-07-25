#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#include <linux/types.h>

extern u64 vli_test_bit(const u64 *vli, unsigned int bit);
extern unsigned int vli_num_digits(const u64 *vli, unsigned int ndigits);
extern unsigned int vli_num_bits(const u64 *vli, unsigned int ndigits);

static const u64 V[6][4] = {
	{0,0,0,0}, {1,0,0,0}, {0xdeadbeefcafef00dULL,0x1234,0,0},
	{0xffffffffffffffffULL,0xffffffffffffffffULL,0,0}, {0,0,0,0x8000000000000000ULL}, {7,7,7,7},
};

static int __init cgir_vli_family_probe_init(void)
{
	u64 digest = 0x9e3779b97f4a7c15ULL;
	int i, b, n;
	for (i = 0; i < 6; i++) {
		for (n = 1; n <= 4; n++) {
			digest ^= (u64)vli_num_digits(V[i], n) + (digest << 6) + (digest >> 2) + n;
			digest ^= (u64)vli_num_bits(V[i], n) * 0x100 + (digest << 6) + (digest >> 2);
		}
		for (b = 0; b < 256; b += 17)
			digest ^= vli_test_bit(V[i], b) + (digest << 5) + (digest >> 3) + b;
	}
	pr_info("CGIR_PROBE vli_family digest=%016llx\n", digest);
	return 0;
}
late_initcall(cgir_vli_family_probe_init);
