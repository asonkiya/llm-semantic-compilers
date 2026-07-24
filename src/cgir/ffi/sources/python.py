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
from dataclasses import dataclass, replace
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


def class_field_types(class_source: str) -> dict[str, str]:
    """``{field: annotation}`` from a class body's ``AnnAssign``s (dataclass /
    pydantic / attrs / plain annotated fields). A non-scalar annotation is kept
    as its unparsed text so field-type resolution rejects it later."""
    try:
        mod = ast.parse(textwrap.dedent(class_source))
    except SyntaxError:
        return {}
    cls = next((n for n in ast.walk(mod) if isinstance(n, ast.ClassDef)), None)
    if cls is None:
        return {}
    out: dict[str, str] = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ann = node.annotation
            out[node.target.id] = ann.id if isinstance(ann, ast.Name) else ast.unparse(ann)
    return out


def _self_field_reads(fn: ast.FunctionDef, self_name: str) -> tuple[set[str], str]:
    """The set of ``self.<field>`` names a pure method reads, or (set(), reason)
    if ``self`` is used any other way — a method call, or ``self`` as a value —
    which means it isn't a pure function of its fields."""
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == self_name
        ):
            return set(), f"calls a method on `{self_name}` (self.{node.func.attr}())"
    fields: set[str] = set()
    attr_value_ids: set[int] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == self_name
        ):
            if isinstance(node.ctx, ast.Store | ast.Del):
                return set(), f"mutates `{self_name}.{node.attr}`"
            fields.add(node.attr)
            attr_value_ids.add(id(node.value))
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == self_name and id(node) not in attr_value_ids:
            return set(), f"`{self_name}` used as a value (not just field reads)"
    return fields, ""


def _typed_param(name: str, ann: str) -> tuple[Param | None, str]:
    p = _param(name, ann)
    if p is None:
        return None, f"unsupported type `{ann}` (need int|float|bool|str|bytes)"
    return p, ""


def parse_signature(
    source: str, symbol: str, class_fields: dict[str, str] | None = None
) -> tuple[Signature | None, str]:
    """(Signature, "") if ``symbol`` in ``source`` has an FFI-eligible ABI, else
    (None, reason). ``source`` is dedented before parsing so an indented snippet
    still parses. A pure method whose ``self`` is only used to read annotated
    scalar/str/bytes fields (``class_fields``) is eligible as a pure function of
    those fields — they become ``from_self`` params."""
    try:
        mod = ast.parse(textwrap.dedent(source))
    except SyntaxError as exc:
        return None, f"unparseable source ({exc.msg})"
    fn = next(
        (
            n
            for n in ast.walk(mod)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == symbol
        ),
        None,
    )
    if fn is None:
        return None, "function definition not found in source"
    if isinstance(fn, ast.AsyncFunctionDef):
        return None, "async function not supported"
    if fn.decorator_list:
        return None, "decorated function"
    a = fn.args
    if a.vararg or a.kwarg:
        return None, "*args/**kwargs not supported"
    if a.kwonlyargs:
        return None, "keyword-only args not supported"
    if a.defaults or any(d is not None for d in a.kw_defaults):
        return None, "default arguments not supported"

    posargs = list(a.posonlyargs) + list(a.args)
    self_param: str | None = None
    from_self: list[Param] = []
    if posargs and posargs[0].arg == "cls":
        return None, "classmethod (cls) not supported"
    if posargs and posargs[0].arg == "self":
        self_param = "self"
        if class_fields is None:
            return None, "method: class field annotations unavailable"
        fields, reason = _self_field_reads(fn, "self")
        if reason:
            return None, reason
        # `fields` may be empty — a method that ignores self is a pure function
        # of its explicit params (the final check rejects only if it has neither).
        for fname in sorted(fields):
            if fname not in class_fields:
                return None, f"self.{fname} is not an annotated class field"
            p, err = _typed_param(fname, class_fields[fname])
            if p is None:
                return None, f"self.{fname}: {err}"
            from_self.append(replace(p, from_self=True))
        posargs = posargs[1:]

    explicit: list[Param] = []
    for arg in posargs:
        if arg.arg in ("self", "cls"):
            return None, "self/cls in a non-receiver position"
        if arg.annotation is None:
            return None, f"param `{arg.arg}` has no type annotation"
        if not isinstance(arg.annotation, ast.Name):
            return None, f"param `{arg.arg}` has a non-scalar type (container/union/Optional)"
        p, err = _typed_param(arg.arg, arg.annotation.id)
        if p is None:
            return None, f"param `{arg.arg}`: {err}"
        explicit.append(p)

    if self_param and not from_self and not explicit:
        return None, "method uses neither self's fields nor any parameter"

    if fn.returns is None:
        return None, "return type annotation missing"
    if isinstance(fn.returns, ast.Constant) and fn.returns.value is None:
        return None, "void return (-> None) has nothing to verify"
    if not isinstance(fn.returns, ast.Name):
        return None, "return type is not a simple name (union/container/Optional)"
    ret = _ret(fn.returns.id)
    if ret is None:
        return None, f"unsupported return type `{fn.returns.id}` (need int|float|bool|str|bytes)"
    return Signature(params=tuple(from_self + explicit), ret=ret, self_param=self_param), ""


def python_rust_worklist(
    index_dir: Path, repo: Path, query: str = "kind:pure"
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
    class_qn: set[str] = set()
    for n in graph["nodes"]:
        q = (n.get("attrs") or {}).get("qualname")
        if q and n.get("path") and n.get("start_line"):
            span[q] = (n["path"], n["start_line"], n.get("end_line") or n["start_line"])
        if q and n.get("kind") == "Class":
            class_qn.add(q)

    def _read_span(qn: str) -> str | None:
        p, st, en = span[qn]
        try:
            return "\n".join((repo / p).read_text().splitlines()[st - 1 : en])
        except OSError:
            return None

    fields_cache: dict[str, dict[str, str]] = {}

    entries: list[PyEntry] = []
    excluded: list[tuple[str, str]] = []
    for s in worklist:
        if s.language != "python":
            excluded.append((s.id, f"not a python function (language={s.language})"))
            continue
        if s.id not in span:
            excluded.append((s.id, "no source span in the graph"))
            continue
        source = _read_span(s.id)
        if source is None:
            excluded.append((s.id, "source file unreadable"))
            continue
        symbol = s.id.rsplit(".", 1)[-1]
        # if this is a method (parent qualname is a class), resolve the class's
        # annotated fields so `self.<field>` reads can be typed.
        parent = s.id.rsplit(".", 1)[0]
        class_fields: dict[str, str] | None = None
        if parent in class_qn and parent in span:
            if parent not in fields_cache:
                csrc = _read_span(parent)
                fields_cache[parent] = class_field_types(csrc) if csrc else {}
            class_fields = fields_cache[parent]
        sig, reason = parse_signature(source, symbol, class_fields)
        if sig is None:
            excluded.append((s.id, reason))
            continue
        entries.append(PyEntry(s.id, symbol, sig, source, span[s.id][0]))
    entries.sort(key=lambda e: e.component_id)
    return entries, excluded
