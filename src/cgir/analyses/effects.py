"""Side-effect classification for Python functions.

Effect taxonomy:
    io              calls into ``print``, ``input``, ``open``
    raise           contains a ``raise`` statement
    net             calls into ``requests`` / ``urllib`` / ``socket`` /
                    ``http.client`` / ``httpx`` / ``aiohttp``
    fs              calls into ``shutil``, destructive ``os.*`` functions,
                    or pathlib-style read/write methods (``.write_text``,
                    ``.read_bytes``, ``.unlink``, ...)
    nondeterm       calls into ``random`` / ``secrets``, clock reads
                    (``time.time``, ``datetime.now``), ``uuid.uuid4``, ...
    calls_effectful (transitive only) — a callee has a direct effect

The transitive tag is split out from the direct ones so :mod:`cgir.slicing`
can distinguish ``effect_adapter`` (does IO itself) from ``orchestrator``
(only routes calls to effectful components).

Detection of ``net`` / ``fs`` / ``nondeterm`` is *lexical*: the dotted
callee text is matched against prefix / suffix tables without symbol
resolution, so ``self.now()`` trips the nondeterm suffix heuristic and an
aliased ``import requests as r`` escapes it. Precision limit, per spec:
flag rather than solve.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.analyses._python_ast import SourceCache, locate_function, python_parser
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind

DIRECT_EFFECT_TAGS: frozenset[str] = frozenset({"io", "raise", "net", "fs", "nondeterm"})
TRANSITIVE_TAG = "calls_effectful"

_IO_BUILTINS: frozenset[str] = frozenset({"print", "input", "open"})

_NET_PREFIXES: tuple[str, ...] = (
    "requests.",
    "urllib.",
    "socket.",
    "http.client.",
    "httpx.",
    "aiohttp.",
)

_FS_PREFIXES: tuple[str, ...] = ("shutil.",)
_FS_EXACT: frozenset[str] = frozenset(
    {
        "os.remove",
        "os.rename",
        "os.replace",
        "os.unlink",
        "os.mkdir",
        "os.makedirs",
        "os.rmdir",
        "os.removedirs",
        "os.chmod",
        "os.chown",
        "os.symlink",
        "os.link",
        "os.truncate",
    }
)
_FS_METHOD_SUFFIXES: tuple[str, ...] = (
    ".write_text",
    ".write_bytes",
    ".read_text",
    ".read_bytes",
    ".unlink",
    ".touch",
)

_NONDETERM_PREFIXES: tuple[str, ...] = ("random.", "secrets.")
_NONDETERM_EXACT: frozenset[str] = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.perf_counter",
        "uuid.uuid1",
        "uuid.uuid4",
        "os.urandom",
        "os.getrandom",
    }
)
_NONDETERM_METHOD_SUFFIXES: tuple[str, ...] = (".now", ".utcnow", ".today")


def classify(graph: RepoGraph, repo_path: Path) -> dict[str, list[str]]:
    """Return ``{function_id: sorted([effect_tag, ...])}`` for every function/method."""
    cache = SourceCache(python_parser(), repo_path)
    func_nodes = [n for n in graph.nodes() if n.kind in {NodeKind.Function, NodeKind.Method}]

    effects: dict[str, set[str]] = {}
    for func in func_nodes:
        effects[func.id] = _direct_effects(cache, func)

    # Propagate transitively over CALLS edges until fixed point.
    changed = True
    while changed:
        changed = False
        for func in func_nodes:
            for edge in graph.out_edges(func.id, EdgeKind.CALLS):
                callee = effects.get(edge.dst, set())
                if (
                    callee & DIRECT_EFFECT_TAGS or TRANSITIVE_TAG in callee
                ) and TRANSITIVE_TAG not in effects[func.id]:
                    effects[func.id].add(TRANSITIVE_TAG)
                    changed = True

    return {nid: sorted(tags) for nid, tags in effects.items()}


def _direct_effects(cache: SourceCache, func: object) -> set[str]:
    # ``func`` is a :class:`cgir.ir.nodes.Node`; typed loosely to avoid an
    # import cycle with the slicer.
    path = getattr(func, "path", None)
    start_line = getattr(func, "start_line", None)
    name = getattr(func, "name", None)
    if path is None or start_line is None or name is None:
        return set()
    parsed = cache.get(path)
    if parsed is None:
        return set()
    source, root = parsed
    func_ts = locate_function(root, name, start_line - 1)
    if func_ts is None:
        return set()
    return _walk_body_for_effects(func_ts, source)


def _walk_body_for_effects(func_ts: TSNode, source: bytes) -> set[str]:
    tags: set[str] = set()
    body = func_ts.child_by_field_name("body")
    if body is None:
        return tags
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "raise_statement":
            tags.add("raise")
        elif node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                name = source[fn.start_byte : fn.end_byte].decode("utf-8", errors="replace")
                if name in _IO_BUILTINS:
                    tags.add("io")
            elif fn is not None and fn.type == "attribute":
                dotted = source[fn.start_byte : fn.end_byte].decode("utf-8", errors="replace")
                tag = _classify_dotted_call(dotted)
                if tag is not None:
                    tags.add(tag)
        stack.extend(node.children)
    return tags


def _classify_dotted_call(dotted: str) -> str | None:
    """Match a dotted callee (``requests.get``, ``p.write_text``) to a tag."""
    if any(ch in dotted for ch in "()[] \n"):
        # A computed receiver (call/subscript chain) — skip rather than guess.
        return None
    if dotted.startswith(_NET_PREFIXES):
        return "net"
    if (
        dotted in _FS_EXACT
        or dotted.startswith(_FS_PREFIXES)
        or dotted.endswith(_FS_METHOD_SUFFIXES)
    ):
        return "fs"
    if (
        dotted in _NONDETERM_EXACT
        or dotted.startswith(_NONDETERM_PREFIXES)
        or dotted.endswith(_NONDETERM_METHOD_SUFFIXES)
    ):
        return "nondeterm"
    return None
