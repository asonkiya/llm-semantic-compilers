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

Detection of ``net`` / ``fs`` / ``nondeterm`` is *lexical*, but
import-alias aware: per-module alias maps (built from the ingester's
Import nodes) normalize ``r.get`` → ``requests.get`` for
``import requests as r`` and resolve bare callees bound by
``from os import remove`` before matching the prefix / suffix tables.
``self.now()`` still trips the nondeterm suffix heuristic — precision
limit, per spec: flag rather than solve.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.analyses._python_ast import SourceCache, locate_function, python_parser
from cgir.analyses.symbols import module_of
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind

DIRECT_EFFECT_TAGS: frozenset[str] = frozenset({"io", "raise", "net", "fs", "nondeterm"})
TRANSITIVE_TAG = "calls_effectful"

_IO_BUILTINS: frozenset[str] = frozenset({"print", "input", "open"})

_NET_PREFIXES: tuple[str, ...] = (
    "requests.",
    # urllib.parse is pure string manipulation — only the request side is net.
    "urllib.request.",
    "urllib.error.",
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
    alias_maps = _module_alias_maps(graph)

    effects: dict[str, set[str]] = {}
    for func in func_nodes:
        module_id = module_of(graph, func)
        aliases = alias_maps.get(module_id, {}) if module_id else {}
        effects[func.id] = _direct_effects(cache, func, aliases)

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


def _module_alias_maps(graph: RepoGraph) -> dict[str, dict[str, str]]:
    """Per module: ``{local_name: absolute_dotted_target}`` from Import nodes."""
    maps: dict[str, dict[str, str]] = {}
    for module in graph.nodes(NodeKind.Module):
        table: dict[str, str] = {}
        for child in graph.children(module.id, NodeKind.Import):
            target = str(child.attrs.get("target") or child.name)
            alias = child.attrs.get("alias")
            local = alias if isinstance(alias, str) else target.rsplit(".", 1)[-1]
            table[local] = target
        maps[module.id] = table
    return maps


def _direct_effects(cache: SourceCache, func: Node, aliases: dict[str, str]) -> set[str]:
    if func.path is None or func.start_line is None:
        return set()
    parsed = cache.get(func.path)
    if parsed is None:
        return set()
    source, root = parsed
    func_ts = locate_function(root, func.name, func.start_line - 1)
    if func_ts is None:
        return set()
    return _walk_body_for_effects(func_ts, source, aliases)


def _walk_body_for_effects(func_ts: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
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
                elif name in aliases:
                    tag = _classify_dotted_call(aliases[name])
                    if tag is not None:
                        tags.add(tag)
            elif fn is not None and fn.type == "attribute":
                dotted = source[fn.start_byte : fn.end_byte].decode("utf-8", errors="replace")
                head, _, rest = dotted.partition(".")
                if rest and head in aliases and aliases[head] != head:
                    dotted = f"{aliases[head]}.{rest}"
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
