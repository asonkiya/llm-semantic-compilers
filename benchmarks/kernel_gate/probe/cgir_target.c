// The C definition of the gate target — present in the STOCK leg only. In the
// rewrite leg this whole file is dropped and a Rust object exporting the same
// `cgir_target` symbol takes its place (obj-y swaps cgir_target.o for the
// shipped Rust object). A real, pure computation (FNV-1a-style mixing over the
// 8 bytes of x): the kind of leaf the pipeline rewrites and the differential
// already verifies in isolation — here proven inside a booting kernel instead.
#include <linux/types.h>

u64 cgir_target(u64 x)
{
	u64 h = 0xcbf29ce484222325ULL;
	int i;

	for (i = 0; i < 8; i++) {
		h ^= (x & 0xff);
		h *= 0x100000001b3ULL;
		x >>= 8;
	}
	return h;
}
