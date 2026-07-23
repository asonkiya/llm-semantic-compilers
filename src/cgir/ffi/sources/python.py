"""Python as a rewrite source: build an FFI worklist of pure, fully-typed leaf
functions from the index, in the shared signature IR (docs/design-ffi-pipeline.md
§6.1).

Eligibility is decided by parsing the function's *real source* with ``ast``
(the stored ``signature`` string is raw tree-sitter text, not reliably
parseable). A function qualifies only if every parameter and the return are
annotated with a bare name in ``int | float | bool | str | bytes`` and it has no
decorators / defaults / ``*args`` / keyword-only args / ``self``. Everything
else is recorded in ``excluded`` with a specific reason, so the dry-run explains
itself.

The behavioral reference for this pair is *captured traces* (``replay.capture``)
replayed against the compiled candidate — see :mod:`cgir.ffi.replay_ffi`.
"""

from __future__ import annotations

import ast
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from cgir.ffi.ir import Param, Signature

# Python annotation name -> canonical scalar name (Param.scalar).
_SCALAR_KINDS = {"int": "i64", "float": "f64", "bool": "bool"}


@dataclass
class PyEntry:
    component_id: str  # qualname, e.g. "mathlib.clamp" — also the trace key
    symbol: str  # the function name / expected no_mangle export
    sig: Signature
    source: str  # the Python source (prompt context)
    path: str  # repo-relative source file (for trace capture targets)


def _param(name: str, ty: str) -> Param | None:
    if ty in _SCALAR_KINDS:
        return Param(name=name, kind="scalar", scalar=_SCALAR_KINDS[ty])
    if ty == "str":
        return Param(name=name, kind="slice", text=True)
    if ty == "bytes":
        return Param(name=name, kind="slice", text=False)
    return None


def _ret(ty: str) -> str | None:
    if ty in _SCALAR_KINDS:
        return _SCALAR_KINDS[ty]
    if ty == "str":
        return "buf:str"
    if ty == "bytes":
        return "buf:bytes"
    return None


def parse_signature(source: str, symbol: str) -> tuple[Signature | None, str]:
    """(Signature, "") if ``symbol`` in ``source`` has an FFI-eligible ABI,
    else (None, reason). ``source`` is dedented before parsing so an indented
    snippet still parses (a method's ``self`` is caught separately)."""
    try:
        mod = ast.parse(textwrap.dedent(source))
    except SyntaxError as exc:
        return None, f"unparseable source ({exc.msg})"
    fn = next(
        (n for n in ast.walk(mod) if isinstance(n, ast.FunctionDef) and n.name == symbol),
        None,
    )
    if fn is None:
        return None, "function definition not found in source"
    if fn.decorator_list:
        return None, "decorated function"
    a = fn.args
    if a.vararg or a.kwarg:
        return None, "*args/**kwargs not supported"
    if a.kwonlyargs:
        return None, "keyword-only args not supported"
    if a.defaults or any(d is not None for d in a.kw_defaults):
        return None, "default arguments not supported"
    params: list[Param] = []
    for arg in list(a.posonlyargs) + list(a.args):
        if arg.arg in ("self", "cls"):
            return None, "method (self/cls) not supported"
        ann = arg.annotation
        if ann is None:
            return None, f"param `{arg.arg}` has no type annotation"
        if not isinstance(ann, ast.Name):
            return None, f"param `{arg.arg}` has a non-scalar type (container/union/Optional)"
        p = _param(arg.arg, ann.id)
        if p is None:
            return (
                None,
                f"param `{arg.arg}`: unsupported type `{ann.id}` (need int|float|bool|str|bytes)",
            )
        params.append(p)
    if fn.returns is None:
        return None, "return type annotation missing"
    if isinstance(fn.returns, ast.Constant) and fn.returns.value is None:
        return None, "void return (-> None) has nothing to verify"
    if not isinstance(fn.returns, ast.Name):
        return None, "return type is not a simple name (union/container/Optional)"
    ret = _ret(fn.returns.id)
    if ret is None:
        return None, f"unsupported return type `{fn.returns.id}` (need int|float|bool|str|bytes)"
    return Signature(params=tuple(params), ret=ret), ""


def python_rust_worklist(
    index_dir: Path, repo: Path, query: str = "kind:pure covered:true"
) -> tuple[list[PyEntry], list[tuple[str, str]]]:
    """Pure, test-covered Python functions with an FFI-eligible ABI.

    Mirrors ``rewrite_repo``'s worklist (``search_specs`` minus test specs) and
    ``c_rust_worklist``'s ``(entries, excluded)`` shape. Reads each function's
    source via the graph span to decide eligibility (and to feed the prompt)."""
    from cgir.export.json_export import read_specs
    from cgir.report.impact import _is_test_spec
    from cgir.report.search import search_specs

    specs = read_specs(index_dir)
    worklist = [s for s in search_specs(specs, query, limit=None) if not _is_test_spec(s)]

    graph = json.loads((index_dir / "repo_graph.json").read_text())
    span: dict[str, tuple[str, int, int]] = {}
    for n in graph["nodes"]:
        q = (n.get("attrs") or {}).get("qualname")
        if q and n.get("path") and n.get("start_line"):
            span[q] = (n["path"], n["start_line"], n.get("end_line") or n["start_line"])

    entries: list[PyEntry] = []
    excluded: list[tuple[str, str]] = []
    for s in worklist:
        if s.language != "python":
            excluded.append((s.id, f"not a python function (language={s.language})"))
            continue
        if s.id not in span:
            excluded.append((s.id, "no source span in the graph"))
            continue
        path, st, en = span[s.id]
        try:
            source = "\n".join((repo / path).read_text().splitlines()[st - 1 : en])
        except OSError:
            excluded.append((s.id, "source file unreadable"))
            continue
        symbol = s.id.rsplit(".", 1)[-1]
        sig, reason = parse_signature(source, symbol)
        if sig is None:
            excluded.append((s.id, reason))
            continue
        entries.append(PyEntry(s.id, symbol, sig, source, path))
    entries.sort(key=lambda e: e.component_id)
    return entries, excluded
