"""Rung 4: C -> Rust cross-language regeneration (vision-rewrite.md).

Worklist: SQLite-amalgamation pure *leaf* functions whose ABI surface is
scalars only. Pipeline per component:

    Haiku writes a #[no_mangle] extern "C" Rust implementation
      -> rustc compiles it                     (filter 1: free, deterministic)
      -> cgir's Rust adapter scans it          (filter 2: cross-language
         contract — pure, arity; REGENERATED_AS recorded in the results)
      -> differential vs the C original        (filter 3: 300 random scalar
         inputs through ctypes, child process so a Rust abort can't kill us)
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
from typing import Any

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
            continue
        ret, name, raw = m.group(1), m.group(2), m.group(3).strip()
        if "*" in raw or "[" in raw or "..." in raw:
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


def rust_signature(e: Entry) -> str:
    args = ", ".join(f"{n}: {TYPE_MAP[t][0]}" for t, n in e.params)
    return f'#[no_mangle]\npub extern "C" fn {e.name}({args}) -> {TYPE_MAP[e.ret][0]}'


def build_prompt(e: Entry) -> str:
    return f"""Translate this C function from SQLite into Rust.

```c
{e.source}
```

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


def differential(orig: Path, cand: Path, e: Entry, n: int, seed: int) -> str:
    """Run trials in a child process; a Rust abort must not kill the harness."""
    spec = {
        "orig": str(orig),
        "cand": str(cand),
        "name": e.name,
        "ret": e.ret,
        "params": [t for t, _ in e.params],
        "n": n,
        "seed": seed,
    }
    proc = subprocess.run(
        [sys.executable, __file__, "--diff-worker"],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        last = proc.stdout.strip().splitlines()
        return f"differential: process died (Rust abort?) near input {last[-1] if last else '?'}"
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    if verdict["status"] == "missing_symbol":
        return "differential: symbol missing from a dylib"
    if verdict["status"] == "mismatch":
        ex = verdict["example"]
        return (
            f"differential mismatch on {verdict['mismatches']}/{verdict['trials']} inputs; "
            f"e.g. {e.name}({ex['args']}): C returned {ex['c']}, Rust returned {ex['rust']}"
        )
    return ""


def diff_worker() -> None:
    import ctypes
    import math
    import random

    spec = json.loads(sys.stdin.read())
    ctype = {t: getattr(ctypes, TYPE_MAP[t][1]) for t in TYPE_MAP}

    o, c = ctypes.CDLL(spec["orig"]), ctypes.CDLL(spec["cand"])
    try:
        fo, fc = getattr(o, spec["name"]), getattr(c, spec["name"])
    except AttributeError:
        print(json.dumps({"status": "missing_symbol"}))
        return
    argtypes = [ctype[t] for t in spec["params"]]
    for f in (fo, fc):
        f.argtypes = argtypes
        f.restype = ctype[spec["ret"]]

    rng = random.Random(spec["seed"])
    EDGE = {
        "c_int": [0, 1, -1, 2**31 - 1, -(2**31)],
        "c_uint": [0, 1, 2**32 - 1],
        "c_short": [0, 1, -1, 32767, -32768],
        "c_ushort": [0, 1, 65535],
        "c_byte": [0, 1, -1, 127, -128],
        "c_ubyte": [0, 1, 255],
        "c_longlong": [0, 1, -1, 2**63 - 1, -(2**63)],
        "c_ulonglong": [0, 1, 2**64 - 1],
        "c_double": [0.0, -0.0, 1.0, -1.0, 1e308, -1e308, 1e-308],
        "c_float": [0.0, 1.0, -1.0],
    }

    def gen(tname: str) -> Any:
        edges = EDGE[tname]
        if rng.random() < 0.25:
            return rng.choice(edges)
        if tname == "c_double":
            return rng.choice([rng.uniform(-1e6, 1e6), rng.uniform(-1e18, 1e18)])
        if tname == "c_float":
            return rng.uniform(-1e6, 1e6)
        lo, hi = min(edges), max(edges)
        small = rng.randint(-256, 256)
        return max(lo, min(hi, small)) if rng.random() < 0.5 else rng.randint(lo, hi)

    mismatches = 0
    example = None
    trials = spec["n"]
    for _ in range(trials):
        args = [gen(TYPE_MAP[t][1]) for t in spec["params"]]
        print(f"TRIAL {args!r}", flush=True)
        rv_o, rv_c = fo(*args), fc(*args)
        if isinstance(rv_o, float):
            same = (math.isnan(rv_o) and math.isnan(rv_c)) or math.isclose(
                rv_o, rv_c, rel_tol=1e-12, abs_tol=0.0
            )
        else:
            same = rv_o == rv_c
        if not same:
            mismatches += 1
            if example is None:
                example = {"args": repr(args), "c": repr(rv_o), "rust": repr(rv_c)}
    status = "mismatch" if mismatches else "equivalent"
    print(
        json.dumps(
            {"status": status, "mismatches": mismatches, "trials": trials, "example": example}
        )
    )


def main() -> None:
    if "--diff-worker" in sys.argv:
        diff_worker()
        return
    import anthropic

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--n-trials", type=int, default=300)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="cgir-rung4-"))
    entries, excluded = build_worklist(args.index, args.src)
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

    client = anthropic.Anthropic()
    ledger = Ledger()
    results: list[Rung4Result] = []
    for i, e in enumerate(entries):
        res = Rung4Result(component_id=e.component_id)
        prompt = build_prompt(e)
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
