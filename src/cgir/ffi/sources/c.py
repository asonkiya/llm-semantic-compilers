"""C as a rewrite source: worklist extraction from the index (signature
regexes → FFI-IR tokens), the compiled behavioral oracle, compiler-probed
context, and apply — patching the translation unit so call sites resolve to
the rewritten symbols (link-back).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from cgir.ffi.ir import CEntry, _toposort
from cgir.ffi.targets.rust import _build_rust_staticlib, extern_block

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
# (C strings) and u8/unsigned char/void* (binary).
_PTR_ELEM = r"(?:char|unsigned\s+char|signed\s+char|u8|i8|void)"
PTR_PARAM = re.compile(rf"^(const\s+)?{_PTR_ELEM}\s*\*\s*(\w+)$")
# A single-level pointer to a named struct — either a typedef (`DateTime *p`)
# or an explicit tag (`struct Ymd *p`). NOT byte-fuzzable (needs a valid
# instance), so struct-pointer functions are verified by the whole-program
# gate on real instances, not the isolated differential. Multi-level (`**`)
# stays out.
STRUCT_PTR = re.compile(r"^(const\s+)?(?:struct\s+|union\s+|enum\s+)?(\w+)\s*\*\s*(\w+)$")

_C_TYPE_WORDS = frozenset(
    ["void", "int", "double", "float", "char", "short", "long", "unsigned", "signed", "const"]
)


def _parse_param(q: str, structs: bool = False) -> tuple[str, str] | None:
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
    if structs:
        sm = STRUCT_PTR.match(q)
        if sm and sm.group(2) not in _C_TYPE_WORDS:
            const = "const" if sm.group(1) else "mut"
            return (f"struct:{sm.group(2)}:{const}", sm.group(3))
    return None


def c_rust_worklist(
    index_dir: Path,
    c_source: Path,
    pointers: bool = False,
    include_nonleaf: bool = False,
    structs: bool = False,
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
                parsed = _parse_param(q, structs=structs)
                if parsed is None:
                    ok = False
                    break
                params.append(parsed)
        has_ptr = any(t.startswith("ptr:") for t, _ in params)
        has_struct = any(t.startswith("struct:") for t, _ in params)
        if not ok:
            excluded.append((s["id"], "unfuzzable parameter (struct/multi-level pointer)"))
            continue
        if has_ptr and not pointers:
            excluded.append((s["id"], "pointer ABI (enable with --pointers)"))
            continue
        if ret == "void" and not has_ptr and not has_struct:
            excluded.append((s["id"], "void return: nothing observable to compare"))
            continue
        if s.get("calls") and not include_nonleaf:
            excluded.append((s["id"], "non-leaf (enable with --non-leaf)"))
            continue
        entry = CEntry(s["id"], name, ret, params, text)
        if has_struct:
            # Struct-pointer functions are verified only by the whole-program
            # gate (real instances); the model writes its own #[repr(C)] mirror
            # from the struct definitions provided in the prompt.
            full = "\n".join(file_cache[path])
            entry.struct_defs = _struct_defs(
                {t.split(":")[1] for t, _ in params if t.startswith("struct:")}, full
            )
        candidates[s["id"]] = entry
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


def _extract_struct(name: str, text: str) -> str | None:
    """The full ``struct <name> { ... }`` body from the source, brace-matched."""
    m = re.search(rf"\bstruct\s+{re.escape(name)}\s*\{{", text)
    if not m:
        return None
    i = text.index("{", m.start())
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                end = text.find(";", j)
                return text[m.start() : (end + 1 if end != -1 else j + 1)]
    return None


def _struct_defs(names: set[str], text: str, depth: int = 2) -> dict[str, str]:
    """Struct definitions for ``names`` plus the struct types they reference,
    up to ``depth`` levels — the layout context the model mirrors as
    ``#[repr(C)]``."""
    out: dict[str, str] = {}
    frontier = set(names)
    for _ in range(depth):
        nxt: set[str] = set()
        for nm in frontier:
            if nm in out:
                continue
            body = _extract_struct(nm, text)
            if body is None:
                continue
            out[nm] = body[:2000]
            # referenced struct types (`struct Foo` or a typedef'd name used as
            # a field type) — shallow, best-effort.
            for ref in re.findall(r"\bstruct\s+(\w+)", body):
                if ref != nm:
                    nxt.add(ref)
        frontier = nxt - set(out)
    return out


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

        # A symbol can have several textual definitions behind mutually
        # exclusive #ifdefs (e.g. sqlite3MemInit's allocator variants). Rename
        # each to a unique sideline so whichever branch compiles no longer
        # exports `name`; the emitted prototype (identical across variants)
        # keeps call sites resolving `name` to the Rust symbol.
        counter = {"n": 0}

        def _rename(m: re.Match[str], _name: str = name, _c: dict[str, int] = counter) -> str:
            _c["n"] += 1
            suffix = "" if _c["n"] == 1 else f"_{_c['n']}"
            proto = f"{m.group(2)} {_name}{m.group(3)};\n"
            return (
                f"{proto}{m.group(1)}{m.group(2)} "
                f"{_name}__cgir_replaced{suffix}{m.group(3)}{m.group(4)}"
            )

        text, n_defs = pattern.subn(_rename, text)
        if n_defs < 1:
            raise ValueError(f"{name}: no definition found to replace")
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
