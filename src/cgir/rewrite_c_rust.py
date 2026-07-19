"""C -> Rust cross-language regeneration — the ``cgir rewrite --lang c-rust``
engine (vision-rewrite.md rung 4).

Given a cgir index and a single compilable C translation unit (an
amalgamation like ``sqlite3.c``, or any one ``.c`` whose worklist symbols it
defines), regenerate its pure functions in Rust and verify each one
mechanically. Leaf-only by default; with ``include_nonleaf`` it also
rewrites functions that call other worklist functions, processed
callees-first — a rewritten Rust caller reaches its callees as ``extern
"C"`` symbols (the original C during verification via the oracle +
RTLD_GLOBAL; the rewritten Rust after link-back). This is the one-pass path:
a whole dependency subgraph rewritten and assembled with all-Rust internal
calls.

    worklist (pure functions with scalar / byte-pointer ABI, from the index)
      -> cheap-model Rust candidate (source + compiler-probed context
         + extern "C" decls for in-repo callees)
      -> rustc                       (compile filter)
      -> cgir Rust-adapter scan       (cross-language contract: pure + arity)
      -> differential vs the compiled C original
         (a fault-trapping C driver; orig-faulting inputs are out-of-contract
          and skipped; pointer params fuzzed with dual buffers + mutation
          compare)
      -> one escalation carrying the compiler error or counterexample

Rides the shared :func:`cgir.rewrite.run_search_loop`, so it inherits the
same k-sampling / escalation / ledger / budget machinery as the Python
``cgir rewrite`` path. Struct-pointer ABIs stay out of scope (they need real
instances); the addressable set is scalar and char*/byte-buffer leaves.

Toolchain: ``cc`` and ``rustc`` on PATH. Network only via the injected
sampler (``--live``).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cgir.rewrite import Sampler, run_search_loop

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
# Read-or-write pointer params fuzzable with a byte buffer: const/mut char*
# (C strings) and u8/unsigned char/void* (binary). Struct and multi-level
# pointers stay out of scope (need real instances).
_PTR_ELEM = r"(?:char|unsigned\s+char|signed\s+char|u8|i8|void)"
PTR_PARAM = re.compile(rf"^(const\s+)?{_PTR_ELEM}\s*\*\s*(\w+)$")

# C type -> (rust type, ctypes name).
TYPE_MAP: dict[str, tuple[str, str]] = {
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
# ctypes-name -> (C concrete type, width bits, is_signed, is_float)
_C_INFO: dict[str, tuple[str, int, int, int]] = {
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


@dataclass
class CEntry:
    component_id: str
    name: str
    ret: str
    params: list[tuple[str, str]]
    source: str
    # in-repo callees also in the worklist — the non-leaf edges. A rewritten
    # Rust caller reaches them as `extern "C"` symbols: the original C during
    # verification (the oracle exports every de-static'd worklist symbol), the
    # rewritten Rust after link-back.
    callees: list[str] = field(default_factory=list)


def _parse_param(q: str) -> tuple[str, str] | None:
    q = q.strip()
    pm = PARAM.match(q)
    if pm:
        return (pm.group(1), pm.group(2))
    pp = PTR_PARAM.match(q)
    if pp:
        is_const = bool(pp.group(1))
        is_str = "char" in q and "unsigned" not in q and "u8" not in q
        kind = "str" if is_str else "buf"
        return (f"ptr:{kind}:{'const' if is_const else 'mut'}", pp.group(2))
    return None


def c_rust_worklist(
    index_dir: Path, c_source: Path, pointers: bool = False, include_nonleaf: bool = False
) -> tuple[list[CEntry], list[tuple[str, str]]]:
    """Pure functions defined in ``c_source`` with a fuzzable ABI.

    Only components whose defining file is ``c_source`` are eligible (their
    symbols must be in the one compiled translation unit we build as the
    behavioral oracle). Leaf-only by default; with ``include_nonleaf`` a
    function is also eligible if *every* in-repo callee is itself in the
    worklist — so the callee is de-static'd/exported and a rewritten caller
    can reach it via ``extern "C"``. Entries carry their ``callees`` and are
    returned callees-first (topological)."""
    graph = json.loads((index_dir / "repo_graph.json").read_text())
    span: dict[str, tuple[str, int, int]] = {}
    for n in graph["nodes"]:
        q = (n.get("attrs") or {}).get("qualname")
        if q and n.get("path"):
            span[q] = (n["path"], n.get("start_line") or 0, n.get("end_line") or 0)
    src_root = _source_root(c_source, span)
    file_cache: dict[str, list[str]] = {}

    # Pass 1: every ABI-eligible pure function (leaf or not), plus its calls.
    candidates: dict[str, CEntry] = {}
    calls_of: dict[str, list[str]] = {}
    excluded: list[tuple[str, str]] = []
    for p in sorted((index_dir / "components").glob("*.json")):
        s = json.loads(p.read_text())
        if s["kind"] != "pure_function" or set(s.get("effects", [])) - {"raise"}:
            continue
        if s["id"] not in span or Path(span[s["id"]][0]).name != c_source.name:
            continue
        path, st, en = span[s["id"]]
        if path not in file_cache:
            file_cache[path] = (src_root / path).read_text().splitlines()
        text = "\n".join(file_cache[path][st - 1 : en])
        header = re.sub(r"\s+", " ", text.split("{")[0]) + "{"
        m = DECL.match(header)
        if not m:
            excluded.append((s["id"], "declaration not scalar-parseable"))
            continue
        ret, name, raw = m.group(1), m.group(2), m.group(3).strip()
        if "[" in raw or "..." in raw:
            excluded.append((s["id"], "array/vararg ABI"))
            continue
        params: list[tuple[str, str]] = []
        ok = True
        if raw not in ("", "void"):
            for q in raw.split(","):
                parsed = _parse_param(q)
                if parsed is None:
                    ok = False
                    break
                params.append(parsed)
        has_ptr = any(t.startswith("ptr:") for t, _ in params)
        if not ok:
            excluded.append((s["id"], "unfuzzable parameter (struct/multi-level pointer)"))
            continue
        if has_ptr and not pointers:
            excluded.append((s["id"], "pointer ABI (enable with --pointers)"))
            continue
        if ret == "void" and not has_ptr:
            excluded.append((s["id"], "void return: nothing observable to compare"))
            continue
        if s.get("calls") and not include_nonleaf:
            excluded.append((s["id"], "non-leaf (enable with --non-leaf)"))
            continue
        candidates[s["id"]] = CEntry(s["id"], name, ret, params, text)
        calls_of[s["id"]] = list(s.get("calls", []))

    # Pass 2: keep functions whose every in-repo callee is also a candidate;
    # record callee *names* for extern generation.
    keep: dict[str, CEntry] = {}
    for cid, e in candidates.items():
        callee_ids = [c for c in calls_of[cid] if c in candidates]
        if len(callee_ids) != len(calls_of[cid]):
            excluded.append((cid, "calls a non-candidate in-repo function"))
            continue
        e.callees = [candidates[c].name for c in callee_ids]
        keep[cid] = e

    return _toposort(keep), excluded


def _toposort(entries: dict[str, CEntry]) -> list[CEntry]:
    """Callees before callers; ties by id. Cycles fall back to id order."""
    name_to_id = {e.name: cid for cid, e in entries.items()}
    order: list[CEntry] = []
    seen: set[str] = set()
    temp: set[str] = set()

    def visit(cid: str) -> None:
        if cid in seen or cid not in entries:
            return
        if cid in temp:  # cycle — leave for id-order fallback
            return
        temp.add(cid)
        for callee_name in entries[cid].callees:
            cbid = name_to_id.get(callee_name)
            if cbid is not None:
                visit(cbid)
        temp.discard(cid)
        seen.add(cid)
        order.append(entries[cid])

    for cid in sorted(entries):
        visit(cid)
    return order


def _source_root(c_source: Path, span: dict[str, tuple[str, int, int]]) -> Path:
    """The repo root the index paths are relative to — the parent of the
    directory chain implied by ``c_source``'s indexed path."""
    for path, _, _ in span.values():
        if Path(path).name == c_source.name:
            rel = Path(path)
            root = c_source.resolve().parent
            for _ in range(len(rel.parts) - 1):
                root = root.parent
            return root
    return c_source.resolve().parent


def compile_oracle(c_source: Path, names: list[str], workdir: Path, flags: list[str]) -> Path:
    """Compile ``c_source`` into a shared library, with the worklist symbols
    de-static'd so they export and are callable as the behavioral oracle."""
    text = c_source.read_text()
    for name in names:
        text = re.sub(
            rf"\bstatic\s+((?:SQLITE_NOINLINE\s+)?(?:const\s+)?{SCALAR_RE}\s+{name}\s*\()",
            r"\1",
            text,
        )
    patched = workdir / "oracle_src.c"
    patched.write_text(text)
    out = workdir / "original.dylib"
    subprocess.run(
        ["cc", "-O1", "-w", "-shared", "-fPIC", *flags, str(patched), "-o", str(out)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return out


def exported_symbols(dylib: Path, names: list[str]) -> set[str]:
    import ctypes

    lib = ctypes.CDLL(str(dylib))
    return {n for n in names if hasattr(lib, n)}


def probe_context(
    c_source: Path, entries: list[CEntry], workdir: Path, flags: list[str]
) -> dict[str, str]:
    """The C compiler as context oracle: probe the real build for macro
    values, ``sizeof``s, and file-scope tables the worklist references, so
    the model never has to guess invisible compile-time facts."""
    probe_src = workdir / "probe_src.c"
    probe_src.write_text(c_source.read_text())
    all_text = probe_src.read_text()

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
        caps |= {w for w in re.findall(r"\b[A-Za-z_]\w*\b", e.source) if w in define_text}
        arrs = {
            a
            for a in re.findall(r"\b([A-Za-z_]\w*)\s*\[", e.source)
            if a not in {n for _, n in e.params} and a not in local_names and not a.isupper()
        }
        szs = set(re.findall(r"sizeof\(\s*(\w+)\s*\)", e.source))
        for name in list(caps):
            body = define_text.get(name, "")
            arrs |= set(re.findall(r"\b([A-Za-z_]\w*)\s*\[", body)) - {"C", "X", "x"}
        wants[e.component_id] = (caps, arrs, szs)
        macros |= caps
        arrays |= arrs
        sizeofs |= szs

    probes: list[tuple[str, str, str]] = []
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

    header = [f'#include "{probe_src.name}"', "#include <stdio.h>", "int main(void) {"]
    values: dict[tuple[str, str], str] = {}
    for _ in range(8):
        lines = header + [p[2] for p in probes] + ["return 0; }"]
        probe_c = workdir / "probe.c"
        probe_c.write_text("\n".join(lines) + "\n")
        proc = subprocess.run(
            ["cc", "-O0", "-w", *flags, str(probe_c), "-o", str(workdir / "probe")],
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
        if len(kept) == len(probes):
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


def _rust_type(token: str) -> str:
    if token.startswith("ptr:"):
        _, _kind, constness = token.split(":")
        return "*const u8" if constness == "const" else "*mut u8"
    return TYPE_MAP[token][0]


def rust_signature(e: CEntry) -> str:
    args = ", ".join(f"{n}: {_rust_type(t)}" for t, n in e.params)
    ret = "" if e.ret == "void" else f" -> {TYPE_MAP[e.ret][0]}"
    return f'#[no_mangle]\npub extern "C" fn {e.name}({args}){ret}'


def extern_block(callees: list[CEntry]) -> str:
    """`extern "C"` declarations so a rewritten caller can call its in-repo
    callees by their C symbol (resolved to the original C during verification,
    the rewritten Rust after link-back)."""
    if not callees:
        return ""
    lines = ['extern "C" {']
    for c in callees:
        args = ", ".join(f"{n}: {_rust_type(t)}" for t, n in c.params)
        ret = "" if c.ret == "void" else f" -> {TYPE_MAP[c.ret][0]}"
        lines.append(f"    fn {c.name}({args}){ret};")
    lines.append("}")
    return "\n".join(lines) + "\n\n"


def build_c_rust_prompt(e: CEntry, context: str = "", callees: list[CEntry] | None = None) -> str:
    ctx = f"\n{context}\n" if context else ""
    ptr_rule = ""
    if any(t.startswith("ptr:") for t, _ in e.params):
        ptr_rule = (
            "\n- Pointer params are raw C pointers into a caller-owned byte buffer "
            "(`*const u8` read-only, `*mut u8` may be written). Use `unsafe` with "
            "explicit bounds — read/write exactly the bytes the C reads/writes, never "
            "past them, and handle a null or zero-length buffer without dereferencing. "
            "A `char*` is a NUL-terminated C string."
        )
    callee_rule = ""
    if callees:
        sigs = "\n".join(f"  {c.name}({', '.join(t for t, _ in c.params)})" for c in callees)
        callee_rule = (
            f"\n- This function calls other functions that are ALREADY available "
            f'via C FFI (declared for you in an `extern "C"` block above your '
            f"function — do not redeclare them): \n{sigs}\n  Call them exactly as the "
            f"C does, inside `unsafe {{ ... }}`. Do NOT reimplement them."
        )
    return f"""Translate this C function into Rust.

```c
{e.source}
```
{ctx}
Contract: deterministic, no I/O, no globals, no heap allocation visible to
the caller. It is called through C FFI; the exact item you must produce is:

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
  that can overflow-panic, no divide-by-zero path C does not have).{ptr_rule}{callee_rule}
- If the C references macros or globals you cannot see, translate the visible
  logic faithfully anyway."""


def try_rustc(
    candidate: str, workdir: Path, tag: str, allow_undefined: bool = False
) -> tuple[Path | None, str]:
    rs = workdir / f"cand_{tag}.rs"
    rs.write_text(candidate + "\n")
    out = workdir / f"cand_{tag}.dylib"
    cmd = ["rustc", "--crate-type=cdylib", "-O", "-o", str(out), str(rs)]
    if allow_undefined:
        # non-leaf candidates reference callees resolved at load time
        import sys

        if sys.platform == "darwin":
            cmd += ["-C", "link-arg=-Wl,-undefined,dynamic_lookup"]
        else:
            cmd += ["-C", "link-arg=-Wl,--allow-shlib-undefined"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return None, "\n".join(proc.stderr.splitlines()[:25])
    return out, ""


def contract_check(candidate: str, e: CEntry, check_purity: bool = True) -> str:
    """Scan the candidate with cgir's Rust adapter: arity must hold, and purity
    unless ``check_purity`` is off (non-leaf callers call known-pure callees
    the adapter can't see through, so the differential judges them)."""
    from cgir.analyses.effects import classify
    from cgir.analyses.symbols import build_symbol_tables
    from cgir.sources import TreeSitterSource

    with tempfile.TemporaryDirectory(prefix="cgir-crust-") as td:
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
        if check_purity and tags:
            return f"contract: candidate is not pure — effects {sorted(tags)}"
        sig = str(node.attrs.get("signature") or "")
        inner = sig.split("(", 1)[1].rsplit(")", 1)[0] if "(" in sig else ""
        arity = len([p for p in inner.split(",") if p.strip()])
        if arity != len(e.params):
            return f"contract: arity {arity} != {len(e.params)} (signature {sig!r})"
    return ""


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
        return f"differential: driver died (rc={run.returncode})"
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


_C_KEYWORDS = frozenset(
    [
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "default",
        "break",
        "continue",
        "return",
        "goto",
        "sizeof",
        "int",
        "unsigned",
        "signed",
        "char",
        "short",
        "long",
        "double",
        "float",
        "void",
        "const",
        "static",
        "struct",
        "union",
        "enum",
        "typedef",
        "volatile",
        "register",
        "extern",
        "inline",
        "NULL",
    ]
)


def suspect_global_reads(e: CEntry) -> set[str]:
    """Heuristic: lowercase identifiers read through ``.``/``->``/``[``/``&``
    that aren't params or locals — the signature of a file-scope global read
    (e.g. ``mem0.nearlyFull``). Such functions are behaviorally equivalent
    only in the state the differential happened to see, so ``--apply``
    excludes them unless ``--force-link``. This is the rung-4-audit
    HeapNearlyFull class, caught statically."""
    params = {n for _, n in e.params}
    # Match a run of type/qualifier tokens then the declared name — so a
    # `static const unsigned char x[]` local table binds `x`, not `char`.
    locals_ = set(
        re.findall(
            r"\b(?:(?:const|static|volatile|unsigned|signed|struct|union|enum|int|char|short|"
            r"long|double|float|void|u8|u16|u32|u64|i8|i16|i32|i64|LogEst|tRowcnt|Bool)\s+)+"
            r"\*?\s*(\w+)",
            e.source,
        )
    )
    accessed = set(re.findall(r"\b([a-z]\w*)\s*(?:\.|->|\[)", e.source))
    accessed |= set(re.findall(r"&\s*([a-z]\w*)\b", e.source))
    return accessed - params - locals_ - _C_KEYWORDS - {e.name}


def _build_rust_staticlib(winners: dict[str, str], workdir: Path, extern_decls: str = "") -> Path:
    lib_rs = workdir / "cgir_rewrites.rs"
    # extern_decls declares any callee that stayed C so a rewritten caller
    # links against it; winner-to-winner calls resolve as crate functions.
    lib_rs.write_text(extern_decls + "\n\n".join(winners[n] for n in sorted(winners)) + "\n")
    out = workdir / "libcgir_rewrites.a"
    subprocess.run(
        ["rustc", "--crate-type=staticlib", "-O", "-o", str(out), str(lib_rs)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return out


def _patch_source(
    c_source: Path, names: list[str], workdir: Path, also_export: set[str] | None = None
) -> Path:
    """Rename each replaced C definition to ``<name>__cgir_replaced`` (with an
    extern prototype, since plain-static functions have no separate
    declaration) and de-static it, so every call site resolves ``<name>`` to
    the Rust symbol at link time. ``also_export`` names are de-static'd but
    *not* renamed — callees that stayed C which a rewritten Rust caller must
    still reach."""
    text = c_source.read_text()
    for name in also_export or set():
        text = re.sub(
            rf"\bstatic\s+((?:SQLITE_NOINLINE\s+)?(?:const\s+)?{SCALAR_RE}\s+{name}\s*\()",
            r"\1",
            text,
        )
    for name in names:
        text = re.sub(
            rf"\bstatic\s+((?:SQLITE_NOINLINE\s+)?(?:const\s+)?{SCALAR_RE}\s+{name}\s*\()",
            r"\1",
            text,
        )
        pattern = re.compile(
            rf"\b((?:SQLITE_PRIVATE\s+|SQLITE_API\s+|SQLITE_NOINLINE\s+)*)"
            rf"({SCALAR_RE})\s+{name}\s*(\([^)]*\))(\s*\{{)"
        )

        def _rename(m: re.Match[str], _name: str = name) -> str:
            proto = f"{m.group(2)} {_name}{m.group(3)};\n"
            return f"{proto}{m.group(1)}{m.group(2)} {_name}__cgir_replaced{m.group(3)}{m.group(4)}"

        text, n_defs = pattern.subn(_rename, text)
        if n_defs != 1:
            raise ValueError(f"{name}: expected exactly 1 definition, patched {n_defs}")
    patched = workdir / (c_source.stem + "_linked.c")
    patched.write_text(text)
    return patched


def link_back(
    c_source: Path,
    winners: dict[str, str],
    out_dir: Path,
    flags: list[str],
    entries: list[CEntry] | None = None,
) -> dict[str, Any]:
    """Assemble ``c_source`` with the winning Rust functions linked in place
    of their C originals. Emits the patched C and the Rust staticlib to
    ``out_dir`` and links a shared library to prove it builds and that the
    symbols resolve to Rust (``nm``). Behavioral equivalence per function was
    already established by the differential.

    With ``entries`` (the worklist), a rewritten caller whose callee stayed C
    still links: those callees are declared ``extern "C"`` in the staticlib
    and de-static'd (not renamed) in the patched source."""
    import shutil

    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="cgir-linkback-"))
    names = sorted(winners)
    # callees of winners that were NOT themselves rewritten -> stay C, but
    # must be reachable (extern-declared in Rust, de-static'd in C).
    still_c: set[str] = set()
    if entries is not None:
        by_name = {e.name: e for e in entries}
        for w in names:
            for callee in by_name.get(w, CEntry("", "", "", [], "")).callees:
                if callee not in winners and callee in by_name:
                    still_c.add(callee)
        extern = extern_block([by_name[c] for c in sorted(still_c)])
    else:
        extern = ""
    staticlib = _build_rust_staticlib(winners, workdir, extern)
    patched = _patch_source(c_source, names, workdir, also_export=still_c)
    shared = out_dir / (c_source.stem + "_rust_inside.dylib")
    # Force the whole Rust archive in even when this TU doesn't itself call
    # the symbol (a linking executable will), so each rewrite is present and
    # nm-verifiable. The flag spelling differs by linker.
    import sys

    if sys.platform == "darwin":
        force = [f"-Wl,-force_load,{staticlib}"]
    else:  # GNU ld
        force = ["-Wl,--whole-archive", str(staticlib), "-Wl,--no-whole-archive"]
    proc = subprocess.run(
        ["cc", "-O1", "-w", "-shared", "-fPIC", *flags, str(patched), *force, "-o", str(shared)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    result: dict[str, Any] = {"functions": names, "linked": proc.returncode == 0}
    if proc.returncode != 0:
        result["error"] = proc.stderr[-2000:]
        return result
    nm = subprocess.run(["nm", str(shared)], capture_output=True, text=True).stdout
    result["symbols_from_rust"] = sum(1 for n in names if re.search(rf"\bT _?{n}\b", nm))
    result["c_definitions_renamed"] = sum(1 for n in names if f"{n}__cgir_replaced" in nm)
    dst_c = out_dir / patched.name
    dst_a = out_dir / staticlib.name
    shutil.copy(patched, dst_c)
    shutil.copy(staticlib, dst_a)
    result["patched_source"] = str(dst_c)
    result["staticlib"] = str(dst_a)
    result["shared_lib"] = str(shared)
    return result


def run_c_rust(
    index_dir: Path,
    c_source: Path,
    *,
    sampler: Sampler,
    c_flags: list[str] | None = None,
    k: int = 3,
    n_trials: int = 300,
    pointers: bool = False,
    include_nonleaf: bool = False,
    budget_usd: float | None = None,
    ledger_path: Path | None = None,
    log: Any = lambda _: None,
) -> dict[str, Any]:
    """Regenerate ``c_source``'s pure functions in Rust, verified end to end.
    With ``include_nonleaf`` the worklist covers functions that call other
    worklist functions, processed callees-first; a rewritten caller reaches
    its callees as ``extern "C"`` symbols. Rides
    :func:`cgir.rewrite.run_search_loop`."""
    flags = c_flags or []
    workdir = Path(tempfile.mkdtemp(prefix="cgir-crust-"))
    entries, excluded = c_rust_worklist(index_dir, c_source, pointers, include_nonleaf)
    orig = compile_oracle(c_source, [e.name for e in entries], workdir, flags)
    have = exported_symbols(orig, [e.name for e in entries])
    for e in entries:
        if e.name not in have:
            excluded.append((e.component_id, "original symbol not exported (platform/#ifdef)"))
    entries = [e for e in entries if e.name in have]
    by_name = {e.name: e for e in entries}
    probe = probe_context(c_source, entries, workdir, flags)
    counter = {"n": 0}

    def _callees(e: CEntry) -> list[CEntry]:
        return [by_name[c] for c in e.callees if c in by_name]

    def make_prompt(e: CEntry) -> str:
        return build_c_rust_prompt(e, probe.get(e.component_id, ""), _callees(e))

    def evaluate(e: CEntry, cand: str) -> tuple[str, str, dict[str, Any]]:
        counter["n"] += 1
        callees = _callees(e)
        # Prepend extern "C" decls so the candidate's calls resolve; they bind
        # to the original C at verify time (oracle, RTLD_GLOBAL) and the
        # rewritten Rust after link-back.
        source = extern_block(callees) + cand
        dylib, err = try_rustc(
            source, workdir, f"{e.name}_{counter['n']}", allow_undefined=bool(callees)
        )
        if dylib is None:
            return "rustc", err, {}
        # Arity always checked; purity only for leaves — a non-leaf's calls to
        # known-pure callees would look impure to the adapter, and the
        # differential is the real behavioral judge either way.
        err = contract_check(cand, e, check_purity=not callees)
        if err:
            return "contract", err, {}
        err = differential(orig, dylib, e, n_trials, seed=42)
        if err:
            return "differential", err, {}
        return "ok", "", {"regenerated_as": f"rust:{e.name}"}

    loop = run_search_loop(
        entries,
        build_prompt=make_prompt,
        evaluate=evaluate,
        sampler=sampler,
        id_of=lambda e: e.component_id,
        k=k,
        budget_usd=budget_usd,
        ledger_path=ledger_path,
        report_meta={"lang": "c-rust", "c_source": str(c_source), "n_trials": n_trials},
        log=log,
    )
    outcomes = loop["outcomes"]
    stage_kills: dict[str, int] = {}
    for o in outcomes:
        for a in o["attempts"]:
            if a["stage"] != "ok":
                stage_kills[a["stage"]] = stage_kills.get(a["stage"], 0) + 1
    loop["excluded"] = [{"id": i, "reason": r} for i, r in excluded]
    loop["stage_kills"] = stage_kills
    loop["results"] = outcomes
    return loop
