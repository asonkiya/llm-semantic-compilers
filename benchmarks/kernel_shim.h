/* Extended kernel shim for the C lifter (benchmarks/c_lift_sweep.py --shim).
 *
 * Provides the *pure-computational* slice of the kernel's in-header vocabulary
 * that a lifted crypto/util function references but the lifter can't reach
 * (it lives in include/, not the .c). Every definition here is either:
 *   - a scalar typedef / errno constant / config macro (value-preserving), or
 *   - a byte-manipulation helper implemented to the kernel's real semantics
 *     (unaligned load/store, byteorder) so the differential compares the Rust
 *     against a *correct* C reference, not a stub.
 *
 * It deliberately does NOT provide allocators (kmalloc/kvfree/vfree),
 * crypto teardown (crypto_free_*), request contexts (aead_request/
 * skcipher_request), or any per-cipher context struct — those are stateful
 * glue, not compute, and shimming them would either be wrong or require full
 * kernel struct layouts. Functions needing them stay (correctly) unliftable.
 */
#ifndef KERNEL_SHIM_H
#define KERNEL_SHIM_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int8_t s8;
typedef int16_t s16;
typedef int32_t s32;
typedef int64_t s64;
typedef uint8_t __u8;
typedef uint16_t __u16;
typedef uint32_t __u32;
typedef uint64_t __u64;
typedef uint16_t __le16, __be16;
typedef uint32_t __le32, __be32;
typedef uint64_t __le64, __be64;

/* annotation / attribute macros — erased */
#define __force
#define __pure
#define __kernel
#define __user
#define __iomem
#define __must_check
#define __always_inline inline
#define ____cacheline_aligned
#define __aligned(x)
#define __packed
#define likely(x) (x)
#define unlikely(x) (x)
#define IS_ENABLED(x) 0
#define fips_enabled 0
#define barrier() ((void)0)

/* errno subset (values match uapi/asm-generic/errno-base.h) */
#define EPERM 1
#define EINTR 4
#define EAGAIN 11
#define ENOMEM 12
#define EACCES 13
#define EBUSY 16
#define EINVAL 22
#define ENOSPC 28
#define ENOLCK 37
#define ENOTSUP 95
#define EOVERFLOW 75
#define EINPROGRESS 115
#define ECANCELED 125

/* min/max/swap/clamp — the kernel's generic helpers */
#define min(a, b) ((a) < (b) ? (a) : (b))
#define max(a, b) ((a) > (b) ? (a) : (b))
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define max_t(t, a, b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define swap(a, b)          \
    do {                    \
        typeof(a) _t = (a); \
        (a) = (b);          \
        (b) = _t;           \
    } while (0)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define round_up(x, y) ((((x) + (y) - 1) / (y)) * (y))
#define round_down(x, y) (((x) / (y)) * (y))
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))

/* bit rotates */
static inline u32 rol32(u32 w, unsigned int s) { return (w << s) | (w >> (32 - s)); }
static inline u32 ror32(u32 w, unsigned int s) { return (w >> s) | (w << (32 - s)); }
static inline u64 rol64(u64 w, unsigned int s) { return (w << s) | (w >> (64 - s)); }
static inline u64 ror64(u64 w, unsigned int s) { return (w >> s) | (w << (64 - s)); }

/* byteorder — identity on LE hosts, but written for correctness either way */
static inline u32 __swab32(u32 x) {
    return ((x & 0xffu) << 24) | ((x & 0xff00u) << 8) | ((x >> 8) & 0xff00u) |
           ((x >> 24) & 0xffu);
}
static inline u16 __swab16(u16 x) { return (u16)((x << 8) | (x >> 8)); }
static inline u64 __swab64(u64 x) {
    return ((u64)__swab32((u32)x) << 32) | __swab32((u32)(x >> 32));
}

/* unaligned little-endian load/store — kernel semantics, byte-exact */
static inline u16 get_unaligned_le16(const void *p) {
    const u8 *b = (const u8 *)p;
    return (u16)(b[0] | (b[1] << 8));
}
static inline u32 get_unaligned_le32(const void *p) {
    const u8 *b = (const u8 *)p;
    return (u32)b[0] | ((u32)b[1] << 8) | ((u32)b[2] << 16) | ((u32)b[3] << 24);
}
static inline u64 get_unaligned_le64(const void *p) {
    const u8 *b = (const u8 *)p;
    return (u64)get_unaligned_le32(b) | ((u64)get_unaligned_le32(b + 4) << 32);
}
static inline void put_unaligned_le16(u16 v, void *p) {
    u8 *b = (u8 *)p;
    b[0] = (u8)v;
    b[1] = (u8)(v >> 8);
}
static inline void put_unaligned_le32(u32 v, void *p) {
    u8 *b = (u8 *)p;
    b[0] = (u8)v;
    b[1] = (u8)(v >> 8);
    b[2] = (u8)(v >> 16);
    b[3] = (u8)(v >> 24);
}
static inline void put_unaligned_le64(u64 v, void *p) {
    put_unaligned_le32((u32)v, p);
    put_unaligned_le32((u32)(v >> 32), (u8 *)p + 4);
}

/* unaligned big-endian */
static inline u16 get_unaligned_be16(const void *p) {
    const u8 *b = (const u8 *)p;
    return (u16)((b[0] << 8) | b[1]);
}
static inline u32 get_unaligned_be32(const void *p) {
    const u8 *b = (const u8 *)p;
    return ((u32)b[0] << 24) | ((u32)b[1] << 16) | ((u32)b[2] << 8) | (u32)b[3];
}
static inline u64 get_unaligned_be64(const void *p) {
    const u8 *b = (const u8 *)p;
    return ((u64)get_unaligned_be32(b) << 32) | get_unaligned_be32(b + 4);
}
static inline void put_unaligned_be32(u32 v, void *p) {
    u8 *b = (u8 *)p;
    b[0] = (u8)(v >> 24);
    b[1] = (u8)(v >> 16);
    b[2] = (u8)(v >> 8);
    b[3] = (u8)v;
}

/* cpu<->endian: identity spellings kernel code uses */
#define cpu_to_le32(x) ((u32)(x))
#define le32_to_cpu(x) ((u32)(x))
#define cpu_to_le16(x) ((u16)(x))
#define le16_to_cpu(x) ((u16)(x))
#define cpu_to_le64(x) ((u64)(x))
#define le64_to_cpu(x) ((u64)(x))
#define cpu_to_be32(x) __swab32((u32)(x))
#define be32_to_cpu(x) __swab32((u32)(x))
#define cpu_to_be16(x) __swab16((u16)(x))
#define be16_to_cpu(x) __swab16((u16)(x))

#endif /* KERNEL_SHIM_H */
