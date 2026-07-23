"""Rust as a rewrite target: FFI signature rendering, rustc compilation,
the cgir Rust-adapter contract scan, and winner assembly (type-item dedup +
staticlib build) for link-back and the whole-program gate.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from cgir.ffi.ir import TYPE_MAP, CEntry, Signature

# The verbatim, experiment-proven skeleton a str/bytes-returning candidate must
# include (M2 fixture: 100k alloc/free cycles clean). `cap` is load-bearing —
# Vec::from_raw_parts with the wrong capacity is UB — so the model gets the
# ManuallyDrop dance handed to it rather than asked to invent it.
RUSTBUF_PRELUDE = """\
#[repr(C)]
pub struct RustBuf { ptr: *mut u8, len: usize, cap: usize }

fn cgir_make_buf(v: Vec<u8>) -> RustBuf {
    let mut v = std::mem::ManuallyDrop::new(v);
    RustBuf { ptr: v.as_mut_ptr(), len: v.len(), cap: v.capacity() }
}

#[no_mangle]
pub extern "C" fn cgir_buf_free(b: RustBuf) {
    if !b.ptr.is_null() { unsafe { drop(Vec::from_raw_parts(b.ptr, b.len, b.cap)); } }
}
"""


def _rust_ret(ret: str) -> str:
    """Rust return spelling for an IR return token: a canonical scalar renders
    as itself; ``buf:*`` (a Rust-allocated string/bytes return) is a RustBuf."""
    return "RustBuf" if ret.startswith("buf:") else ret


def rust_signature_ir(symbol: str, sig: Signature) -> str:
    """The exact ``#[no_mangle] extern "C"`` signature a Python->Rust candidate
    must produce. Scalars (``i64``/``f64``/``bool``) render as themselves; a
    slice param expands to a ``(ptr, len)`` pair; a ``buf:*`` return is a
    RustBuf (see :data:`RUSTBUF_PRELUDE`)."""
    parts: list[str] = []
    for p in sig.params:
        if p.kind == "scalar":
            parts.append(f"{p.name}: {p.scalar}")
        else:  # slice -> (ptr, len)
            parts.append(f"{p.name}_ptr: *const u8")
            parts.append(f"{p.name}_len: usize")
    args = ", ".join(parts)
    ret = "" if sig.ret == "void" else f" -> {_rust_ret(sig.ret)}"
    return f'#[no_mangle]\npub extern "C" fn {symbol}({args}){ret}'


def _rust_type(token: str) -> str:
    if token.startswith("ptr:"):
        _, _kind, constness = token.split(":")
        return "*const u8" if constness == "const" else "*mut u8"
    if token.startswith("struct:"):
        _, name, constness = token.split(":")
        return f"*const {name}" if constness == "const" else f"*mut {name}"
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


def try_rustc(
    candidate: str,
    workdir: Path,
    tag: str,
    allow_undefined: bool = False,
    extra_flags: list[str] | None = None,
) -> tuple[Path | None, str]:
    rs = workdir / f"cand_{tag}.rs"
    rs.write_text(candidate + "\n")
    out = workdir / f"cand_{tag}.dylib"
    cmd = ["rustc", "--crate-type=cdylib", "-O", "-o", str(out), str(rs), *(extra_flags or [])]
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


# Head of a top-level type-defining Rust item, keyed by name. Struct-pointer
# winners each emit a `#[repr(C)]` mirror of the SAME C struct, so two winners
# sharing a struct would define it twice in one crate. We dedup these by name.
_RUST_TYPE_HEAD = re.compile(
    r"^(?:pub\s+)?(?:struct|enum|union|type)\s+(\w+)",
    re.MULTILINE,
)


def _split_rust_items(src: str) -> list[str]:
    """Split a Rust source fragment into top-level items by brace matching.

    Skips braces inside line/block comments and string/char literals so a `}`
    in a function body or string doesn't end an item early. Good enough for the
    model-generated `#[repr(C)]` struct + function shape we assemble; it only
    feeds type-item dedup, and a mis-split at worst falls back to today's
    behavior (a duplicate-definition compile error, no silent miscompile)."""
    items: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        two = src[i : i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if c == "'":
            # char literal `'x'` / `'\n'`; a lifetime `'a` has no closing quote
            # nearby, so only skip when a close quote is within 3 chars.
            close = src.find("'", i + 1)
            if 0 < close <= i + 3:
                i = close + 1
                continue
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                items.append(src[start : i + 1])
                j = i + 1
                while j < n and src[j] in " \t\r\n;":
                    j += 1
                start = j
                i = j
                continue
        i += 1
    tail = src[start:].strip()
    if tail:
        items.append(tail)
    return [it.strip() for it in items if it.strip()]


def _assemble_winner_bodies(winners: dict[str, str]) -> str:
    """Concatenate winner sources into one crate body, deduping shared
    type-defining items (a `#[repr(C)]` struct mirror emitted by more than one
    struct-pointer winner) by name — first definition wins. Functions stay at
    top-level scope so winner-to-winner (non-leaf) crate calls still resolve.

    When no winner defines a type (the scalar/pointer path), this is a no-op:
    the original flat concatenation is returned unchanged."""
    ordered = [winners[n] for n in sorted(winners)]
    if not any(_RUST_TYPE_HEAD.search(w) for w in ordered):
        return "\n\n".join(ordered)
    seen_types: set[str] = set()
    type_items: list[str] = []
    other_items: list[str] = []
    for w in ordered:
        for item in _split_rust_items(w):
            m = _RUST_TYPE_HEAD.search(item)
            if m:
                if m.group(1) not in seen_types:
                    seen_types.add(m.group(1))
                    type_items.append(item)
            else:
                other_items.append(item)
    return "\n\n".join(type_items + other_items)


# The prelude helper functions (as opposed to the RustBuf *type*, which
# `_RUST_TYPE_HEAD` already dedups). Multiple string-returning Python->Rust
# winners each emit the whole prelude, so these collide on assembly.
_PRELUDE_FNS = frozenset({"cgir_make_buf", "cgir_buf_free"})
_RUST_FN_HEAD = re.compile(r"\bfn\s+(\w+)")


def assemble_python_winners(winners: dict[str, str]) -> str:
    """Concatenate Python->Rust winners into one crate, deduping the shared
    RustBuf prelude (the ``#[repr(C)]`` struct *and* the ``cgir_make_buf`` /
    ``cgir_buf_free`` helpers) by name — first wins — while keeping every
    winner function. Robust to per-candidate whitespace drift in the prelude
    (dedup is by item name, not text)."""
    seen: set[tuple[str, str]] = set()
    types: list[str] = []
    helpers: list[str] = []
    others: list[str] = []
    for sym in sorted(winners):
        for item in _split_rust_items(winners[sym]):
            tm = _RUST_TYPE_HEAD.search(item)
            if tm:
                key = ("type", tm.group(1))
                if key not in seen:
                    seen.add(key)
                    types.append(item)
                continue
            fm = _RUST_FN_HEAD.search(item)
            if fm and fm.group(1) in _PRELUDE_FNS:
                key = ("fn", fm.group(1))
                if key not in seen:
                    seen.add(key)
                    helpers.append(item)
                continue
            others.append(item)
    return "\n\n".join(types + helpers + others)


def _build_rust_staticlib(winners: dict[str, str], workdir: Path, extern_decls: str = "") -> Path:
    lib_rs = workdir / "cgir_rewrites.rs"
    # extern_decls declares any callee that stayed C so a rewritten caller
    # links against it; winner-to-winner calls resolve as crate functions.
    body = _assemble_winner_bodies(winners)
    if not body:  # empty set (the gate's stock baseline) — keep the crate valid
        body = '#[no_mangle]\npub extern "C" fn __cgir_gate_empty() {}'
    lib_rs.write_text(extern_decls + body + "\n")
    out = workdir / "libcgir_rewrites.a"
    subprocess.run(
        ["rustc", "--crate-type=staticlib", "-O", "-o", str(out), str(lib_rs)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return out
