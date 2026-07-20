"""The dylib-vs-dylib differential — the FFI core's isolated behavioral check.

Language-neutral by construction: the generated C driver dlopens two shared
libraries (the behavioral reference and the candidate), fuzzes inputs over the
FFI-IR param tokens, and compares returns + post-call buffer bytes under a
fault trap. It never reads the source language — any pair whose two sides
expose the same C-ABI symbol rides this unchanged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cgir.ffi.ir import _C_INFO, TYPE_MAP, CEntry


def exported_symbols(dylib: Path, names: list[str]) -> set[str]:
    import ctypes

    lib = ctypes.CDLL(str(dylib))
    return {n for n in names if hasattr(lib, n)}


def _driver_source(e: CEntry) -> str:
    """Self-contained fault-trapping differential driver for one function.

    dlopen's both libraries; a sigaltstack + sigaction(SA_ONSTACK) handler
    guarded by sigsetjmp turns SIGSEGV/SIGABRT into a recorded trap. If the C
    *original* faults on an input, that input is out-of-contract and skipped;
    a candidate faulting where the original ran cleanly is a real divergence.
    Pointer params get separate identical buffers for orig and candidate, and
    equivalence requires matching return AND matching post-call buffer bytes.
    """
    sig_types: list[str] = []
    globals_: list[str] = []
    fills: list[str] = []
    decls: list[str] = []
    orig_args: list[str] = []
    cand_args: list[str] = []
    buf_cmps: list[str] = []
    printf_fmt: list[str] = []
    printf_args: list[str] = []
    for i, (token, _name) in enumerate(e.params):
        if token.startswith("ptr:"):
            _, kind, constness = token.split(":")
            cty = "const uint8_t*" if constness == "const" else "uint8_t*"
            sig_types.append(cty)
            globals_.append(f"static uint8_t BO_{i}[BUFSZ]; static uint8_t BC_{i}[BUFSZ];")
            filler = "fill_str" if kind == "str" else "fill_buf"
            fills.append(f"        {filler}(BO_{i}); memcpy(BC_{i}, BO_{i}, BUFSZ);")
            orig_args.append(f"({cty})BO_{i}")
            cand_args.append(f"({cty})BC_{i}")
            buf_cmps.append(f"memcmp(BO_{i}, BC_{i}, BUFSZ)==0")
            printf_fmt.append("buf")
        else:
            ctype, bits, signed, isflt = _C_INFO[TYPE_MAP[token][1]]
            if isflt:
                decls.append(f"        {ctype} a{i} = ({ctype})rndd();")
                printf_fmt.append("%g")
                printf_args.append(f"(double)a{i}")
            else:
                decls.append(f"        {ctype} a{i} = ({ctype})rnd({bits}, {signed});")
                printf_fmt.append("%lld")
                printf_args.append(f"(long long)a{i}")
            sig_types.append(ctype)
            orig_args.append(f"a{i}")
            cand_args.append(f"a{i}")
    sig = ", ".join(sig_types) or "void"
    fmt = ",".join(printf_fmt)
    exargs = (", " + ", ".join(printf_args)) if printf_args else ""
    bufs_ok = " && ".join(buf_cmps) if buf_cmps else "1"

    if e.ret == "void":
        ret_c = "void"
        ret_decl_o, ret_decl_c = "", ""
        call_o = f"fo({', '.join(orig_args)});"
        call_c = f"fc({', '.join(cand_args)});"
        ret_eq = "1"
        ex_fault = ex_mism = f'"{e.name}({fmt}) buffers differ"'
        ex_fault_args = ex_mism_args = exargs
    else:
        ret_c, _, _, ret_float = _C_INFO[TYPE_MAP[e.ret][1]]
        ret_decl_o, ret_decl_c = f"{ret_c} ro; ", f"{ret_c} rc; "
        call_o = f"ro = fo({', '.join(orig_args)});"
        call_c = f"rc = fc({', '.join(cand_args)});"
        if ret_float:
            ret_eq = (
                "((isnan(ro)&&isnan(rc)) || ro==rc || "
                "fabs((double)ro-(double)rc) <= 1e-9*fmax(fabs((double)ro),fabs((double)rc)))"
            )
            rfmt, ro_a, rc_a = "%g", "(double)ro", "(double)rc"
        else:
            ret_eq = "(ro==rc)"
            rfmt, ro_a, rc_a = "%lld", "(long long)ro", "(long long)rc"
        ex_fault = f'"{e.name}({fmt}) orig={rfmt} rust=FAULT"'
        ex_fault_args = f"{exargs}, {ro_a}"
        ex_mism = f'"{e.name}({fmt}) orig={rfmt} rust={rfmt}"'
        ex_mism_args = f"{exargs}, {ro_a}, {rc_a}"

    return f"""
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <dlfcn.h>
#include <setjmp.h>
#include <signal.h>
#include <math.h>

#define BUFSZ 4096
typedef {ret_c} (*fn_t)({sig});
{chr(10).join(globals_)}
static sigjmp_buf JB;
static volatile sig_atomic_t FAULT;
static char ALTSTK[SIGSTKSZ * 4];
static void on_fault(int s) {{ FAULT = s; siglongjmp(JB, 1); }}
static void install_handlers(void) {{
    stack_t ss;
    ss.ss_sp = ALTSTK; ss.ss_size = sizeof ALTSTK; ss.ss_flags = 0;
    sigaltstack(&ss, 0);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_fault;
    sa.sa_flags = SA_ONSTACK | SA_NODEFER;
    sigemptyset(&sa.sa_mask);
    int sigs[] = {{SIGSEGV, SIGBUS, SIGABRT, SIGFPE, SIGILL}};
    for (unsigned k = 0; k < sizeof sigs / sizeof sigs[0]; k++) sigaction(sigs[k], &sa, 0);
}}

static uint64_t S;
static uint64_t xr(void) {{ S ^= S<<13; S ^= S>>7; S ^= S<<17; return S ? S : (S=0x9E3779B97F4A7C15ULL); }}
static int64_t rnd(int bits, int is_signed) {{
    uint64_t r = xr();
    int mode = r % 10;
    int64_t v;
    if (mode < 3)      v = (int64_t)(xr() % 513) - 256;
    else if (mode < 5) {{ int64_t e[] = {{0,1,-1}}; v = e[xr()%3]; }}
    else               v = (int64_t)xr();
    if (bits < 64) {{
        uint64_t mask = ((uint64_t)1 << bits) - 1;
        uint64_t m = ((uint64_t)v) & mask;
        v = (is_signed && (m >> (bits-1))) ? (int64_t)(m | ~mask) : (int64_t)m;
    }}
    return v;
}}
static double rndd(void) {{
    uint64_t r = xr();
    int mode = r % 12;
    double e[] = {{0.0,-0.0,1.0,-1.0,1e308,-1e308,1e-308,
                  (double)INFINITY,-(double)INFINITY,(double)NAN,
                  9.2233720368547758e18,-9.2233720368547758e18}};
    if (mode < 5) return e[xr()%12];
    double base = mode < 9 ? 1e6 : 1e18;
    return ((double)(int64_t)xr() / (double)INT64_MAX) * base;
}}
static void fill_buf(uint8_t* b) {{ for (long j=0;j<BUFSZ;j++) b[j]=(uint8_t)xr(); }}
static void fill_str(uint8_t* b) {{
    long L = xr() % 65;
    memset(b, 0, BUFSZ);
    for (long j=0;j<L;j++) b[j] = (uint8_t)(33 + xr()%94);
    b[L] = 0;
}}

int main(int argc, char** argv) {{
    if (argc < 5) return 2;
    long n = atol(argv[3]);
    S = strtoull(argv[4], 0, 10);
    /* GLOBAL so the oracle's de-static'd symbols satisfy a non-leaf
       candidate's extern callees, resolving them to the original C. */
    void* ho = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
    void* hc = dlopen(argv[2], RTLD_NOW);
    if (!ho || !hc) {{ printf("{{\\"status\\":\\"dlopen_fail\\"}}\\n"); return 0; }}
    fn_t fo = (fn_t)dlsym(ho, "{e.name}");
    fn_t fc = (fn_t)dlsym(hc, "{e.name}");
    if (!fo || !fc) {{ printf("{{\\"status\\":\\"missing_symbol\\"}}\\n"); return 0; }}
    install_handlers();

    long compared=0, mism=0, orig_faults=0, cand_faults=0, both_faults=0;
    char example[600]="";
    for (long i=0; i<n; i++) {{
{chr(10).join(decls)}
{chr(10).join(fills)}
        {ret_decl_o}int of=0;
        if (sigsetjmp(JB,1)==0) {{ {call_o} }} else of=1;
        {ret_decl_c}int cf=0;
        if (sigsetjmp(JB,1)==0) {{ {call_c} }} else cf=1;
        if (of) {{ if (cf) both_faults++; else orig_faults++; continue; }}
        if (cf) {{
            cand_faults++; mism++;
            if (!example[0]) snprintf(example,sizeof example,
                {ex_fault}{ex_fault_args});
            continue;
        }}
        compared++;
        if (!({ret_eq} && ({bufs_ok}))) {{
            mism++;
            if (!example[0]) snprintf(example,sizeof example,
                {ex_mism}{ex_mism_args});
        }}
    }}
    printf("{{\\"status\\":\\"%s\\",\\"compared\\":%ld,\\"mismatches\\":%ld,"
           "\\"orig_faults\\":%ld,\\"cand_faults\\":%ld,\\"both_faults\\":%ld,"
           "\\"example\\":\\"%s\\"}}\\n",
        mism?"mismatch":"equivalent", compared, mism, orig_faults,
        cand_faults, both_faults, example);
    return 0;
}}
"""


def differential(orig: Path, cand: Path, e: CEntry, n: int, seed: int) -> str:
    """Compile the fault-trapping driver and run it; returns "" on
    equivalence or a human-readable divergence/inconclusive reason."""
    trials = n if e.params else 1
    drv_c = cand.with_suffix(".driver.c")
    drv = cand.with_suffix(".driver")
    drv_c.write_text(_driver_source(e))
    try:
        comp = subprocess.run(
            ["cc", "-O0", "-w", str(drv_c), "-o", str(drv)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "differential: driver compile timed out"
    if comp.returncode != 0:
        return f"differential: driver compile failed:\n{comp.stderr[:300]}"
    try:
        run = subprocess.run(
            [str(drv), str(orig), str(cand), str(trials), str(seed or 1)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # A candidate that never terminates on some input (e.g. a rewritten
        # strcmp that walks past a non-NUL-terminated buffer) is a rejection,
        # not a crash of the whole run — the loop escalates or moves on.
        return "differential: candidate timed out (likely non-terminating on some input)"
    if run.returncode != 0:
        return f"differential: driver died (rc={run.returncode})"
    v = json.loads(run.stdout.strip().splitlines()[-1])
    if v["status"] in ("missing_symbol", "dlopen_fail"):
        return f"differential: {v['status']}"
    compared = v["compared"]
    # Inputs the fuzzer put out of contract: the original faults (orig-only or
    # alongside the candidate). A high rate with almost no clean comparisons
    # means the valid-input manifold is something random bytes essentially can't
    # hit — a precondition like "i, j, g index a valid pattern" that only the
    # real caller enforces (ts_bm's subpattern). Then neither a clean pass nor a
    # mismatch is trustworthy (the mismatches are on UB garbage the C read
    # without faulting), so report inconclusive and let the caller defer to the
    # whole-program gate rather than reject a possibly-correct translation.
    faulted = v.get("orig_faults", 0) + v.get("both_faults", 0)
    if e.params and faulted >= trials // 2 and compared < trials // 4:
        return (
            f"differential inconclusive: only {compared} in-contract inputs of "
            f"{trials} ({faulted} out-of-contract faults in the C original)"
        )
    if v["status"] == "mismatch":
        return (
            f"differential mismatch on {v['mismatches']} inputs "
            f"({compared} compared, {v['cand_faults']} candidate-faults); "
            f"e.g. {v['example']}"
        )
    return ""
