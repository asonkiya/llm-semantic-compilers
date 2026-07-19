"""Rung 4: C -> Rust cross-language regeneration (vision-rewrite.md).

Worklist: SQLite-amalgamation pure *leaf* functions whose ABI surface is
scalars only. Pipeline per component:

    Haiku writes a #[no_mangle] extern "C" Rust implementation
      -> rustc compiles it                     (filter 1: free, deterministic)
      -> cgir's Rust adapter scans it          (filter 2: cross-language
         contract — pure, arity; REGENERATED_AS recorded in the results)
      -> differential vs the C original        (filter 3: random scalar
         inputs via a compiled, fault-trapping C driver — SIGSEGV/SIGABRT
         become recorded traps, not process deaths; orig-faulting inputs
         are out-of-contract and skipped)
      -> failures escalate once to Sonnet with the compiler error or the
         counterexample.

The C oracle is the amalgamation itself compiled once with
-DSQLITE_PRIVATE= (plus de-static'ing worklist symbols) so originals are
callable directly — no source extraction, no reimplementation drift.

Run from the cgir repo venv:
    .venv/bin/python benchmarks/rung4_c_to_rust.py \
        --src <sqlite-src> --index <sqlite-idx> --out results.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Reuse the rung-3 generation/ledger machinery (same directory).
sys.path.insert(0, str(Path(__file__).parent))
from rung3_rewrite import CHEAP_MODEL, ESCALATION_MODEL, Ledger, _generate


def _extract(text: str) -> str:
    """Language-agnostic fence stripper (rung3's is Python-specific)."""
    m = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("#[", "pub ", "fn ", "extern ")):
            return "\n".join(lines[i:])
    return text


SCALAR_RE = (
    r"(?:const\s+)?(?:unsigned\s+|signed\s+)?"
    r"(?:void|int|double|float|char|short|long(?:\s+long)?|"
    r"u8|u16|u32|u64|i8|i16|i32|i64|sqlite3_u?int64|sqlite_u?int64|"
    r"LogEst|tRowcnt|Bool)"
)
DECL = re.compile(
    rf"^(?:static\s+|SQLITE_PRIVATE\s+|SQLITE_API\s+|SQLITE_NOINLINE\s+)*"
    rf"({SCALAR_RE})\s+(\w+)\s*\(([^)]*)\)\s*\{{",
    re.DOTALL,
)
PARAM = re.compile(rf"^({SCALAR_RE})\s+(\w+)$")

# C type -> (rust type, ctypes name). Typedefs resolved against the
# amalgamation: LogEst=short, tRowcnt=u64, Bool=unsigned, u64=8-byte.
TYPE_MAP = {
    "int": ("i32", "c_int"),
    "i32": ("i32", "c_int"),
    "unsigned": ("u32", "c_uint"),
    "unsigned int": ("u32", "c_uint"),
    "Bool": ("u32", "c_uint"),
    "u32": ("u32", "c_uint"),
    "double": ("f64", "c_double"),
    "float": ("f32", "c_float"),
    "char": ("i8", "c_byte"),
    "signed char": ("i8", "c_byte"),
    "i8": ("i8", "c_byte"),
    "unsigned char": ("u8", "c_ubyte"),
    "u8": ("u8", "c_ubyte"),
    "short": ("i16", "c_short"),
    "i16": ("i16", "c_short"),
    "LogEst": ("i16", "c_short"),
    "u16": ("u16", "c_ushort"),
    "long": ("i64", "c_longlong"),
    "long long": ("i64", "c_longlong"),
    "i64": ("i64", "c_longlong"),
    "sqlite3_int64": ("i64", "c_longlong"),
    "sqlite_int64": ("i64", "c_longlong"),
    "unsigned long": ("u64", "c_ulonglong"),
    "unsigned long long": ("u64", "c_ulonglong"),
    "u64": ("u64", "c_ulonglong"),
    "sqlite3_uint64": ("u64", "c_ulonglong"),
    "sqlite_uint64": ("u64", "c_ulonglong"),
    "tRowcnt": ("u64", "c_ulonglong"),
}


@dataclass
class Entry:
    component_id: str
    name: str
    ret: str
    params: list[tuple[str, str]]
    source: str
    lines: int


@dataclass
class Attempt:
    model: str
    candidate: str = ""
    stage: str = ""  # rustc | contract | differential | ok
    feedback: str = ""


@dataclass
class Rung4Result:
    component_id: str
    solved_by: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    excluded: str = ""
    regenerated_as: str = ""


def build_worklist(index: Path, src: Path) -> tuple[list[Entry], list[tuple[str, str]]]:
    graph = json.loads((index / "repo_graph.json").read_text())
    span: dict[str, tuple[str, int, int]] = {}
    for n in graph["nodes"]:
        q = (n.get("attrs") or {}).get("qualname")
        if q and n.get("path"):
            span[q] = (n["path"], n.get("start_line") or 0, n.get("end_line") or 0)
    file_cache: dict[str, list[str]] = {}
    entries: list[Entry] = []
    excluded: list[tuple[str, str]] = []
    for p in sorted((index / "components").glob("*.json")):
        s = json.loads(p.read_text())
        if s["kind"] != "pure_function" or s.get("calls") or set(s.get("effects", [])) - {"raise"}:
            continue
        if s["id"] not in span:
            continue
        path, st, en = span[s["id"]]
        if path != "sqlite3.c":
            if path == "shell.c":
                excluded.append((s["id"], "shell.c statics are not exportable"))
            continue
        if path not in file_cache:
            file_cache[path] = (src / path).read_text().splitlines()
        text = "\n".join(file_cache[path][st - 1 : en])
        header = re.sub(r"\s+", " ", text.split("{")[0]) + "{"
        m = DECL.match(header)
        if not m:
            excluded.append((s["id"], "declaration not scalar-parseable"))
            continue
        ret, name, raw = m.group(1), m.group(2), m.group(3).strip()
        if "*" in raw or "[" in raw or "..." in raw:
            excluded.append((s["id"], "non-scalar parameters (pointer/array/vararg ABI)"))
            continue
        if ret == "void":
            excluded.append((s["id"], "void return: nothing observable to compare"))
            continue
        params: list[tuple[str, str]] = []
        ok = True
        if raw not in ("", "void"):
            for q in raw.split(","):
                pm = PARAM.match(q.strip())
                if not pm:
                    ok = False
                    break
                params.append((pm.group(1), pm.group(2)))
        if not ok:
            excluded.append((s["id"], "non-scalar parameters (pointer/array/vararg ABI)"))
            continue
        entries.append(Entry(s["id"], name, ret, params, text, en - st + 1))
    return entries, excluded


def compile_original(src: Path, names: list[str], workdir: Path) -> Path:
    """Amalgamation -> dylib with worklist symbols exported."""
    text = (src / "sqlite3.c").read_text()
    for name in names:
        text = re.sub(
            rf"\bstatic\s+((?:SQLITE_NOINLINE\s+)?(?:const\s+)?{SCALAR_RE}\s+{name}\s*\()",
            r"\1",
            text,
        )
    patched = workdir / "sqlite3_patched.c"
    patched.write_text(text)
    out = workdir / "original.dylib"
    subprocess.run(
        [
            "cc",
            "-O1",
            "-w",
            "-shared",
            "-fPIC",
            "-DSQLITE_PRIVATE=",
            # optional subsystems whose pure leaves are on the worklist
            "-DSQLITE_ENABLE_FTS3",
            "-DSQLITE_ENABLE_FTS5",
            "-DSQLITE_ENABLE_RTREE",
            str(patched),
            "-o",
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return out


def exported_symbols(dylib: Path, names: list[str]) -> set[str]:
    import ctypes

    lib = ctypes.CDLL(str(dylib))
    return {n for n in names if hasattr(lib, n)}


def probe_compile_time_context(src: Path, entries: list[Entry], workdir: Path) -> dict[str, str]:
    """The C compiler as context oracle.

    Rung 4's misses were invisible compile-time facts: macro values,
    ``sizeof`` of internal structs, file-scope lookup tables. Instead of
    modeling the preprocessor, ask the build itself: generate one probe
    program that ``#include``s the amalgamation and prints every value the
    worklist references, iteratively dropping probes the compiler rejects
    (locals, non-macros, function-like uses). Returns component_id ->
    rendered context block for the prompt.
    """
    amalg = workdir / "sqlite3_probe.c"
    amalg.write_text((src / "sqlite3.c").read_text())
    all_text = amalg.read_text()

    # #define texts (with backslash continuations) for names used
    # function-like or where a numeric probe fails — the definition itself
    # is context.
    define_text: dict[str, str] = {}
    for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(.*(?:\\\n.*)*)", all_text, re.M):
        define_text.setdefault(m.group(1), f"#define {m.group(1)}{m.group(2)}"[:300])

    wants: dict[str, tuple[set[str], set[str], set[str]]] = {}
    macros: set[str] = set()
    arrays: set[str] = set()
    sizeofs: set[str] = set()
    for e in entries:
        local_names = set(
            re.findall(r"\b(?:(?!return\b|case\b|goto\b)[a-z]\w*\s+)+(\w+)\s*(?:=|;|\[)", e.source)
        )
        caps = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", e.source)) - {"NULL"}
        # mixed-case macros (IdChar, ROUND8-style) — anything the amalgamation
        # #defines that this source mentions
        caps |= {w for w in re.findall(r"\b[A-Za-z_]\w*\b", e.source) if w in define_text}
        arrs = {
            a
            for a in re.findall(r"\b([A-Za-z_]\w*)\s*\[", e.source)
            if a not in {n for _, n in e.params} and a not in local_names and not a.isupper()
        }
        szs = set(re.findall(r"sizeof\(\s*(\w+)\s*\)", e.source))
        # One level of macro recursion: names inside the matched #define
        # bodies (IdChar -> sqlite3CtypeMap) are context too.
        for name in list(caps):
            body = define_text.get(name, "")
            arrs |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\[", body)) - {"C", "X", "x"}
        wants[e.component_id] = (caps, arrs, szs)
        macros |= caps
        arrays |= arrs
        sizeofs |= szs

    probes: list[tuple[str, str, str]] = []  # (kind, name, C line)
    for name in sorted(macros):
        probes.append(("MACRO", name, f'printf("MACRO {name} %lld\\n", (long long)({name}));'))
    for name in sorted(sizeofs):
        probes.append(("SIZEOF", name, f'printf("SIZEOF {name} %zu\\n", sizeof({name}));'))
    for name in sorted(arrays):
        probes.append(
            (
                "ARRAY",
                name,
                f"{{ size_t n = sizeof({name})/sizeof({name}[0]); "
                f'printf("ARRAY {name} %zu ", n); '
                f'for (size_t i = 0; i < n && i < 512; i++) printf("%lld,", (long long)({name}[i])); '
                f'printf("\\n"); }}',
            )
        )

    header = ['#include "sqlite3_probe.c"', "#include <stdio.h>", "int main(void) {"]
    values: dict[tuple[str, str], str] = {}
    for _ in range(8):
        lines = header + [p[2] for p in probes] + ["return 0; }"]
        probe_c = workdir / "probe.c"
        probe_c.write_text("\n".join(lines) + "\n")
        proc = subprocess.run(
            [
                "cc",
                "-O0",
                "-w",
                "-DSQLITE_ENABLE_FTS3",
                "-DSQLITE_ENABLE_FTS5",
                "-DSQLITE_ENABLE_RTREE",
                str(probe_c),
                "-o",
                str(workdir / "probe"),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=workdir,
        )
        if proc.returncode == 0:
            break
        bad_lines = {
            int(m.group(1))
            for m in re.finditer(rf"{re.escape(probe_c.name)}:(\d+):\d+:\s*error", proc.stderr)
        }
        bad_idx = {ln - len(header) - 1 for ln in bad_lines}
        kept = [p for i, p in enumerate(probes) if i not in bad_idx]
        if len(kept) == len(probes):  # can't attribute the error; give up on probing
            probes = []
            break
        probes = kept
    else:
        probes = []
    if probes:
        run = subprocess.run([str(workdir / "probe")], capture_output=True, text=True, timeout=60)
        for line in run.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3 and parts[0] in ("MACRO", "SIZEOF", "ARRAY"):
                values[(parts[0], parts[1])] = line

    context: dict[str, str] = {}
    for e in entries:
        caps, arrs, szs = wants[e.component_id]
        out: list[str] = []
        for name in sorted(caps):
            if ("MACRO", name) in values:
                out.append(f"{name} = {values[('MACRO', name)].split(' ', 2)[2]}")
            elif name in define_text:
                out.append(define_text[name])
        for name in sorted(szs):
            if ("SIZEOF", name) in values:
                out.append(f"sizeof({name}) = {values[('SIZEOF', name)].split(' ', 2)[2]}")
        for name in sorted(arrs):
            if ("ARRAY", name) in values:
                _, _, rest = values[("ARRAY", name)].partition(f"ARRAY {name} ")
                n, _, elems = rest.partition(" ")
                out.append(f"static table {name}[{n}] = {{{elems.rstrip(',')}}}")
        if out:
            context[e.component_id] = (
                "Known compile-time values, probed from the real build "
                "(trust these over guesses):\n" + "\n".join(f"  {line}" for line in out)
            )
    return context


def rust_signature(e: Entry) -> str:
    args = ", ".join(f"{n}: {TYPE_MAP[t][0]}" for t, n in e.params)
    return f'#[no_mangle]\npub extern "C" fn {e.name}({args}) -> {TYPE_MAP[e.ret][0]}'


def build_prompt(e: Entry, context: str = "") -> str:
    ctx = f"\n{context}\n" if context else ""
    return f"""Translate this C function from SQLite into Rust.

```c
{e.source}
```
{ctx}
Contract: pure function — deterministic, no I/O, no globals, no allocation
visible to the caller. It is called through C FFI; the exact item you must
produce is:

{rust_signature(e)} {{
    ...
}}

Rules:
- Output ONLY that one function item, no markdown fences, no `use` statements,
  no extra items, no comments about the translation.
- Preserve C semantics exactly: two's-complement wrapping arithmetic where C
  could overflow (use wrapping_add/wrapping_mul/wrapping_shl etc.), C
  integer-division/shift behavior, and identical branch conditions.
- The function must never panic for ANY input (no unwrap, no plain arithmetic
  that can overflow-panic, no divide-by-zero path C does not have).
- If the C references macros or globals you cannot see, translate the visible
  logic faithfully anyway."""


def try_rustc(candidate: str, workdir: Path, tag: str) -> tuple[Path | None, str]:
    rs = workdir / f"cand_{tag}.rs"
    rs.write_text(candidate + "\n")
    out = workdir / f"cand_{tag}.dylib"
    proc = subprocess.run(
        ["rustc", "--crate-type=cdylib", "-O", "-o", str(out), str(rs)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return None, "\n".join(proc.stderr.splitlines()[:25])
    return out, ""


def contract_check(candidate: str, e: Entry) -> str:
    """Scan the candidate with cgir's Rust adapter: pure + arity must hold."""
    from cgir.analyses.effects import classify
    from cgir.analyses.symbols import build_symbol_tables
    from cgir.sources import TreeSitterSource

    with tempfile.TemporaryDirectory(prefix="cgir-rung4-") as td:
        d = Path(td)
        (d / "lib.rs").write_text(candidate + "\n")
        graph = TreeSitterSource().ingest(d)
        build_symbol_tables(graph)
        effects = classify(graph, d)
        node = next(
            (
                n
                for n in graph.nodes()
                if n.attrs.get("qualname", "").endswith(e.name) and n.kind.value == "Function"
            ),
            None,
        )
        if node is None:
            return f"contract: function `{e.name}` not found in candidate"
        tags = set(effects.get(node.id, {})) - {"raise"}
        if tags:
            return f"contract: candidate is not pure — effects {sorted(tags)}"
        sig = str(node.attrs.get("signature") or "")
        inner = sig.split("(", 1)[1].rsplit(")", 1)[0] if "(" in sig else ""
        arity = len([p for p in inner.split(",") if p.strip()])
        if arity != len(e.params):
            return f"contract: arity {arity} != {len(e.params)} (signature {sig!r})"
    return ""


# ctypes-name -> (C concrete type, width bits, is_signed, is_float)
_C_INFO = {
    "c_int": ("int32_t", 32, 1, 0),
    "c_uint": ("uint32_t", 32, 0, 0),
    "c_short": ("int16_t", 16, 1, 0),
    "c_ushort": ("uint16_t", 16, 0, 0),
    "c_byte": ("int8_t", 8, 1, 0),
    "c_ubyte": ("uint8_t", 8, 0, 0),
    "c_longlong": ("int64_t", 64, 1, 0),
    "c_ulonglong": ("uint64_t", 64, 0, 0),
    "c_double": ("double", 64, 1, 1),
    "c_float": ("float", 32, 1, 1),
}


def _driver_source(e: Entry) -> str:
    """Generate a self-contained C driver for one function.

    dlopen's both libraries, installs a fault handler, and runs trials with
    each call guarded by sigsetjmp so a SIGSEGV/SIGABRT becomes a recorded
    trap, not a process death. Contract-aware semantics: if the C *original*
    faults on an input, that input is out-of-contract (the C itself has UB
    there) and is skipped; only the candidate faulting where the original
    did not is a real divergence.
    """
    cinfo = [_C_INFO[TYPE_MAP[t][1]] for t, _ in e.params]
    ret_c, _, _, ret_float = _C_INFO[TYPE_MAP[e.ret][1]]
    sig = ", ".join(c[0] for c in cinfo) or "void"
    decls, args, printf_fmt, printf_args = [], [], [], []
    for i, (ctype, bits, signed, isflt) in enumerate(cinfo):
        if isflt:
            decls.append(f"    {ctype} a{i} = ({ctype})rndd();")
            printf_fmt.append("%g")
        else:
            decls.append(f"    {ctype} a{i} = ({ctype})rnd({bits}, {signed});")
            printf_fmt.append("%lld")
            printf_args.append(f"(long long)a{i}")
        args.append(f"a{i}")
        if isflt:
            printf_args.append(f"(double)a{i}")
    call_args = ", ".join(args)
    fmt = ",".join(printf_fmt)
    exargs = (", " + ", ".join(printf_args)) if printf_args else ""
    if ret_float:
        eq = (
            "(isnan(ro)&&isnan(rc)) || ro==rc || "
            "fabs((double)ro-(double)rc) <= 1e-9*fmax(fabs((double)ro),fabs((double)rc))"
        )
        ret_fmt, ro_arg, rc_arg = "%g", "(double)ro", "(double)rc"
    else:
        eq = "ro==rc"
        ret_fmt, ro_arg, rc_arg = "%lld", "(long long)ro", "(long long)rc"
    return f"""
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <dlfcn.h>
#include <setjmp.h>
#include <signal.h>
#include <math.h>

typedef {ret_c} (*fn_t)({sig});
static sigjmp_buf JB;
static volatile sig_atomic_t FAULT;
static void on_fault(int s) {{ FAULT = s; siglongjmp(JB, 1); }}

static uint64_t S;
static uint64_t xr(void) {{ S ^= S<<13; S ^= S>>7; S ^= S<<17; return S ? S : (S=0x9E3779B97F4A7C15ULL); }}
static int64_t rnd(int bits, int is_signed) {{
    uint64_t r = xr();
    int mode = r % 10;
    int64_t v;
    if (mode < 3)      v = (int64_t)(xr() % 513) - 256;      /* small */
    else if (mode < 5) {{ int64_t e[] = {{0,1,-1}}; v = e[xr()%3]; }}
    else               v = (int64_t)xr();                    /* full width */
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

int main(int argc, char** argv) {{
    if (argc < 5) return 2;
    long n = atol(argv[3]);
    S = strtoull(argv[4], 0, 10);
    void* ho = dlopen(argv[1], RTLD_NOW);
    void* hc = dlopen(argv[2], RTLD_NOW);
    if (!ho || !hc) {{ printf("{{\\"status\\":\\"dlopen_fail\\"}}\\n"); return 0; }}
    fn_t fo = (fn_t)dlsym(ho, "{e.name}");
    fn_t fc = (fn_t)dlsym(hc, "{e.name}");
    if (!fo || !fc) {{ printf("{{\\"status\\":\\"missing_symbol\\"}}\\n"); return 0; }}
    signal(SIGSEGV, on_fault); signal(SIGBUS, on_fault);
    signal(SIGABRT, on_fault); signal(SIGFPE, on_fault); signal(SIGILL, on_fault);

    long compared=0, mism=0, orig_faults=0, cand_faults=0, both_faults=0;
    char example[600]=""; 
    for (long i=0; i<n; i++) {{
{chr(10).join(decls)}
        {ret_c} ro; int of=0;
        if (sigsetjmp(JB,1)==0) {{ ro = fo({call_args}); }} else of=1;
        {ret_c} rc; int cf=0;
        if (sigsetjmp(JB,1)==0) {{ rc = fc({call_args}); }} else cf=1;
        if (of) {{ if (cf) both_faults++; else orig_faults++; continue; }}
        if (cf) {{
            cand_faults++; mism++;
            if (!example[0]) snprintf(example,sizeof example,
                "{e.name}({fmt}) orig={ret_fmt} rust=FAULT"{exargs}, {ro_arg});
            continue;
        }}
        compared++;
        if (!({eq})) {{
            mism++;
            if (!example[0]) snprintf(example,sizeof example,
                "{e.name}({fmt}) orig={ret_fmt} rust={ret_fmt}"{exargs}, {ro_arg}, {rc_arg});
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


def differential(orig: Path, cand: Path, e: Entry, n: int, seed: int) -> str:
    """Compile a fault-trapping C driver and run it. Orig-faulting inputs are
    out-of-contract and skipped; a candidate faulting where the original ran
    is a real divergence. Requires a minimum of valid comparisons to avoid a
    vacuous pass on functions whose contract domain we rarely hit."""
    trials = n if e.params else 1
    drv_c = cand.with_suffix(".driver.c")
    drv = cand.with_suffix(".driver")
    drv_c.write_text(_driver_source(e))
    comp = subprocess.run(
        ["cc", "-O0", "-w", str(drv_c), "-o", str(drv)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if comp.returncode != 0:
        return f"differential: driver compile failed:\n{comp.stderr[:300]}"
    run = subprocess.run(
        [str(drv), str(orig), str(cand), str(trials), str(seed or 1)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if run.returncode != 0:
        return f"differential: driver died (rc={run.returncode}) — should not happen"
    v = json.loads(run.stdout.strip().splitlines()[-1])
    if v["status"] in ("missing_symbol", "dlopen_fail"):
        return f"differential: {v['status']}"
    if v["status"] == "mismatch":
        return (
            f"differential mismatch on {v['mismatches']} inputs "
            f"({v['compared']} compared, {v['cand_faults']} candidate-faults); "
            f"e.g. {v['example']}"
        )
    if e.params and v["compared"] < max(20, trials // 10) and v["orig_faults"] > 0:
        return (
            f"differential inconclusive: only {v['compared']} in-contract inputs "
            f"({v['orig_faults']} out-of-contract faults in the C original)"
        )
    return ""


def main() -> None:
    import anthropic

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--n-trials", type=int, default=300)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--recheck",
        type=Path,
        default=None,
        help="Re-verify the winners in a prior results file under the current "
        "(harsher) differential; no generation, no API cost.",
    )
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="cgir-rung4-"))
    entries, excluded = build_worklist(args.index, args.src)

    if args.recheck:
        prior = json.loads(args.recheck.read_text())
        by_id = {e.component_id: e for e in entries}
        orig = compile_original(args.src, [e.name for e in entries], workdir)
        flips = []
        for r in prior["results"]:
            if not r["solved_by"]:
                continue
            e = by_id[r["component_id"]]
            winner = next(a["candidate"] for a in r["attempts"] if a["stage"] == "ok")
            dylib, err = try_rustc(winner, workdir, f"rc_{e.name}")
            verdict = err or differential(orig, dylib, e, args.n_trials, seed=7)
            status = "still-equivalent" if not verdict else "FLIPPED"
            if verdict:
                flips.append((r["component_id"], verdict))
            print(f"{status:17s} {r['component_id']:55s} {verdict[:90]}", flush=True)
        print(f"\n{len(flips)} winner(s) flipped under the harsher differential")
        args.out.write_text(json.dumps({"rechecked": True, "flips": flips}, indent=2) + "\n")
        return
    if args.limit:
        entries = entries[: args.limit]
    print(f"worklist: {len(entries)} scalar-ABI pure leaves; compiling C oracle...", flush=True)
    t0 = time.monotonic()
    orig = compile_original(args.src, [e.name for e in entries], workdir)
    have = exported_symbols(orig, [e.name for e in entries])
    for e in entries:
        if e.name not in have:
            excluded.append((e.component_id, "original symbol not exported (platform/#ifdef)"))
    entries = [e for e in entries if e.name in have]
    print(
        f"oracle compiled in {time.monotonic() - t0:.0f}s; {len(entries)} originals callable",
        flush=True,
    )
    t0 = time.monotonic()
    probe_ctx = probe_compile_time_context(args.src, entries, workdir)
    print(
        f"compile-time context probed in {time.monotonic() - t0:.0f}s "
        f"({len(probe_ctx)}/{len(entries)} components enriched)",
        flush=True,
    )

    client = anthropic.Anthropic()
    ledger = Ledger()
    results: list[Rung4Result] = []
    for i, e in enumerate(entries):
        res = Rung4Result(component_id=e.component_id)
        prompt = build_prompt(e, probe_ctx.get(e.component_id, ""))
        t0 = time.monotonic()
        for model, tier, n in ((CHEAP_MODEL, "cheap", args.k), (ESCALATION_MODEL, "escalation", 1)):
            if tier == "escalation":
                fb = next((a.feedback for a in reversed(res.attempts) if a.feedback), "")
                if not fb:
                    break
                prompt = (
                    f"{prompt}\n\nA previous attempt failed:\n{fb}\n\n"
                    "Produce a corrected implementation."
                )
            for j, cand in enumerate(_generate(client, model, prompt, n, ledger)):
                cand = _extract(cand)
                at = Attempt(model=model, candidate=cand)
                res.attempts.append(at)
                dylib, err = try_rustc(cand, workdir, f"{i}_{len(res.attempts)}_{j}")
                if dylib is None:
                    at.stage, at.feedback = "rustc", err
                    continue
                err = contract_check(cand, e)
                if err:
                    at.stage, at.feedback = "contract", err
                    continue
                err = differential(orig, dylib, e, args.n_trials, seed=42)
                if err:
                    at.stage, at.feedback = "differential", err
                    continue
                at.stage = "ok"
                res.solved_by = tier
                res.regenerated_as = f"rust:{e.name}"
                break
            if res.solved_by:
                break
        status = res.solved_by or "unsolved"
        print(
            f"{e.component_id:55s} {status:11s} attempts={len(res.attempts)} "
            f"{time.monotonic() - t0:5.1f}s ${ledger.cost():.3f} cum",
            flush=True,
        )
        results.append(res)

    solved_cheap = sum(r.solved_by == "cheap" for r in results)
    solved_esc = sum(r.solved_by == "escalation" for r in results)
    stage_kills: dict[str, int] = {}
    for r in results:
        for a in r.attempts:
            if a.stage != "ok":
                stage_kills[a.stage] = stage_kills.get(a.stage, 0) + 1
    report = {
        "src": str(args.src),
        "k": args.k,
        "n_trials": args.n_trials,
        "models": {"cheap": CHEAP_MODEL, "escalation": ESCALATION_MODEL},
        "worklist": len(entries),
        "excluded": [{"id": i, "reason": r} for i, r in excluded],
        "solved_cheap": solved_cheap,
        "solved_escalation": solved_esc,
        "unsolved": len(entries) - solved_cheap - solved_esc,
        "plug_in_rate": round((solved_cheap + solved_esc) / len(entries), 3) if entries else None,
        "stage_kills": stage_kills,
        "cost_usd": {
            "cheap": round(ledger.cost(CHEAP_MODEL), 4),
            "escalation": round(ledger.cost(ESCALATION_MODEL), 4),
            "total": round(ledger.cost(), 4),
        },
        "tokens": ledger.tokens,
        "results": [asdict(r) for r in results],
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps({k: v for k, v in report.items() if k not in ("results", "excluded")}, indent=2)
    )


if __name__ == "__main__":
    main()
