# ☀️ MORNING SUMMARY — read this first

**Spent ~$0.40 of $10.** Everything below is committed + pushed (CGIR `main`, lockstep repo).

## 1. Companion project shipped
`github.com/asonkiya/lockstep` (private) — full design doc for region-level verified
transplant of *concurrent* C into Rust-for-Linux (the residue CGIR can't reach), gated by
KCSAN/lockdep/syzkaller instead of byte-identity. Flip public: `gh repo edit asonkiya/lockstep --visibility public`.

## 2. HEADLINE — real Linux kernel functions, model-rewritten to Rust, verified INSIDE a booting kernel
Last session's rung-4 used a *planted* function. Tonight: **4 real crypto functions**,
model-generated Rust, kbuild-compiled, boot-verified — across 2 subsystems + 2 ABI classes,
with negative controls proving the gate isn't vacuous:

| function | subsystem | verdict |
|---|---|---|
| mul_by_x  | AES GF(2⁸)         | PASS |
| mul_by_x2 | AES GF(2⁸)         | PASS |
| subw      | AES S-box (256-entry table embedded by the model) | PASS |
| vli_cmp   | ECC big-int (gate-required u64* class) | PASS |
| mul_by_x / vli_cmp (deliberately wrong) | negative controls | REJECT |

4/4 correct verified, 2/2 wrong rejected, 0 false results.

## 3. Key finding — the kernel strategy pivot
The "57 header-lifted" number was measured with **macOS cc**, which collides Darwin vs
kernel types. Lifting-to-standalone-TU is the WRONG tool for the kernel — kbuild already
owns the toolchain/headers. The right kernel path is **rewrite-in-place + boot-gate** (what
the 4 results above do). Lifter's real domain stays userspace amalgamations (SQLite).

## 4. Hygiene
Full suite 776 passed, ruff+mypy clean — no regressions from the header-aware/struct-enum/
closure-cap changes. Kernel TU battery: 35/46 solved, $0.31, 0 errors.

---
## Detailed log

# Overnight run — 2026-07-25 (started ~03:45)

$10 API budget loaded. Goal: hardcore testing, results readable in the morning.
Companion repo `lockstep` created + design doc pushed (private).

## Campaigns
1. Kernel header-aware TU live battery — rewrite all 57 lifted kernel crypto TUs.
2. Shim/header dedup lever — drop shim lines the closure defines; re-sweep (rung-3 +N).
3. Real kernel function in-kernel gate — pick a battery winner, verify it booting.

## Log

### [03:50] Campaign 2 finding — cross-compile wall (pivot)
Probed why ~630 header-lifted kernel TUs fail to compile. Root cause is NOT shim
dedup — it's that the sweep compiles kernel C with **macOS `cc`**: the shim's
`#include <stdint.h>` pulls Darwin libc headers that collide with kernel types
(`__kernel_ssize_t`=int vs `__darwin_ssize_t`), plus unstripped annotation macros
(`__rcu`) and function-pointer typedefs (`lock_cmp_fn`). No-shim fails the other
way (u32/u64 undefined — pulled types not fully ordered).

**Conclusion: lifting-to-standalone-TU is the wrong tool for the kernel.** It
fights the header ecosystem. The in-place kbuild gate (rung 4) is right: kbuild
already has the correct toolchain, flags (`-nostdinc -ffreestanding -D__KERNEL__`),
and header tree. The lifter's real domain stays userspace amalgamations (SQLite).
So the "57 lift-compilable (macOS cc)" is an artifact of the wrong compiler, not
the real kernel-rewritable surface. PIVOT: scale the in-kernel gate to real
functions instead of chasing standalone-TU compiles.

### [04:00] Campaign 1 running + Campaign 3 launched
- Kernel battery: solving real AES primitives (mix_columns, mul_by_x, subw all solved).
  Some pathological differential drivers hit the alarm(90) cap — bounded, not wedged.
- gen_rust: model rewrote mul_by_x + mul_by_x2 (real kernel AES GF math) to Rust.
- Campaign 3 (headline): generalized in-kernel gate to real named functions;
  mul_by_x gate running (model's Rust vs C, boot digest). Negative control
  (wrong reduction poly 0x1c) staged.

### [04:20] HEADLINE — real kernel function verified in-kernel
GATE PASS (mul_by_x): the model's Rust rewrite of the REAL Linux AES GF(2^8)
routine `mul_by_x` (crypto/aes_generic.c) computes byte-identically to the C,
inside a booting kernel. digest=574c81fc56c361fb (both legs). This is the
upgrade from last session's PLANTED function — now a load-bearing kernel
primitive, model-generated, kernel-toolchain-compiled, boot-verified.
Running now: negative control (wrong reduction poly 0x1c → must REJECT) +
mul_by_x2 (second real AES routine → must PASS).
Battery: ~15/57, healthy, $0.03 spent (vli_* solve as gate-required).

### [04:35] Real-function kernel gate — 2 PASS + negative control REJECT
- mul_by_x  : PASS (574c81fc56c361fb)  — real AES GF(2^8) mul-by-x
- mul_by_x2 : PASS (f4d92da8c6a53765)  — real AES GF(2^8) mul-by-x^2
- mul_by_x wrong (poly 0x1c): REJECT (c8612be0e2da12fc != stock) — gate discriminates
Two real Linux kernel functions, model-generated Rust, kernel-toolchain-built,
boot-verified. The gate is not vacuous (control rejects). Next: subw (table-driven,
reads the AES S-box) to prove a table-embedding rewrite works in-kernel too.

### [04:55] subw PASS — table-driven real function verified in-kernel
GATE PASS (subw): 2798f60600c34773 (both legs). The model embedded the full
256-entry AES S-box; kbuild compiled it; boot verified it byte-identical to the
C table lookup. This proves the lifter's CORE value (tree-shake a table into the
rewrite) works end-to-end in the REAL kernel.

SCOREBOARD (real Linux kernel functions, model Rust, verified inside booting kernel):
  mul_by_x   PASS   (GF mul-by-x, no table)
  mul_by_x2  PASS   (GF mul-by-x^2, no table)
  subw       PASS   (S-box word transform, 256-entry table embedded)
  mul_by_x*  REJECT (wrong reduction poly — negative control)
3/3 correct rewrites verified, 1/1 wrong rewrite rejected. 0 false results.

### [05:05] Kernel header-TU battery DONE
57 TUs, 0 errors, 46 fn components, 35 solved (76%; 25 gate-required = vli_*/struct),
31 targets, $0.31, 677s. Note: these are ISOLATED-TU rewrites (macOS-cc verified for
the differential ones); the IN-KERNEL gate (above) is the authoritative kernel proof.
Next: non-leaf real function (mix_columns calls mul_by_x) via the in-kernel gate.
