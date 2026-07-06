"""PythonAdapter — the Python implementation of :class:`LanguageAdapter`.

Holds everything grammar- or stdlib-specific to Python: tree-sitter-python
parsing, the effect-detection tables, and call-site extraction. The
analysis algorithms that consume these live language-neutrally in
``cgir/analyses``.
"""

from __future__ import annotations

import tree_sitter_python
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cgir.languages.base import CallSite, LanguageAdapter

# --- effect detection tables (Python stdlib + common CV/ML libs) --------------

_IO_BUILTINS: frozenset[str] = frozenset({"print", "input", "open"})
_IO_DOTTED_EXACT: frozenset[str] = frozenset(
    {
        "cv2.VideoCapture",
        "cv2.VideoWriter",
        "cv2.imshow",
        "cv2.waitKey",
        "cv2.destroyAllWindows",
    }
)
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
        # path-taking media / model IO (CV & ML codebases)
        "cv2.imread",
        "cv2.imwrite",
        "torch.load",
        "torch.save",
        "np.load",
        "np.save",
        "np.savez",
        "numpy.load",
        "numpy.save",
        "numpy.savez",
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
_NONDETERM_PREFIXES: tuple[str, ...] = (
    "random.",
    "secrets.",
    "np.random.",
    "numpy.random.",
    "torch.rand",
)
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

_DB_RECEIVERS: frozenset[str] = frozenset(
    {"db", "database", "session", "conn", "connection", "cursor", "engine", "tx", "txn"}
)
_DB_METHODS: frozenset[str] = frozenset(
    {
        "add",
        "add_all",
        "begin",
        "commit",
        "delete",
        "execute",
        "executemany",
        "fetchall",
        "fetchmany",
        "fetchone",
        "flush",
        "get",
        "merge",
        "query",
        "refresh",
        "rollback",
        "scalar",
        "scalars",
    }
)


class PythonAdapter(LanguageAdapter):
    name = "python"
    file_extensions = (".py",)

    def __init__(self) -> None:
        language = Language(tree_sitter_python.language())
        self._parser = Parser()
        self._parser.language = language

    def parse(self, source: bytes) -> TSNode:
        return self._parser.parse(source).root_node

    def locate_function(self, root: TSNode, name: str, start_row: int) -> TSNode | None:
        stack: list[TSNode] = [root]
        while stack:
            node = stack.pop()
            if node.type == "function_definition" and node.start_point[0] == start_row:
                name_node = node.child_by_field_name("name")
                if (
                    name_node is not None
                    and name_node.text is not None
                    and name_node.text.decode("utf-8", errors="replace") == name
                ):
                    return node
            stack.extend(node.children)
        return None

    def direct_effects(self, func_node: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
        tags: set[str] = set()
        body = func_node.child_by_field_name("body")
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
                    name = _text(fn, source)
                    if name in _IO_BUILTINS:
                        tags.add("io")
                    elif name in aliases:
                        tag = _classify_dotted_call(aliases[name])
                        if tag is not None:
                            tags.add(tag)
                elif fn is not None and fn.type == "attribute":
                    dotted = _text(fn, source)
                    head, _, rest = dotted.partition(".")
                    if rest and head in aliases and aliases[head] != head:
                        dotted = f"{aliases[head]}.{rest}"
                    tag = _classify_dotted_call(dotted)
                    if tag is not None:
                        tags.add(tag)
            stack.extend(node.children)
        return tags

    def call_sites(self, func_node: TSNode, source: bytes) -> list[CallSite]:
        sites: list[CallSite] = []
        body = func_node.child_by_field_name("body")
        if body is None:
            return sites
        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()
            if node.type == "call":
                function_field = node.child_by_field_name("function")
                if function_field is not None:
                    if function_field.type == "identifier":
                        decoded: str | None = _text(function_field, source)
                    elif function_field.type == "attribute":
                        decoded = _text(function_field, source)
                        if "(" in decoded or "[" in decoded or "\n" in decoded:
                            # Computed receiver: keep just the head identifier.
                            decoded = decoded.split(".", 1)[0]
                    else:
                        decoded = None
                    if decoded:
                        arguments = node.child_by_field_name("arguments")
                        args = _arg_names(arguments, source) if arguments is not None else []
                        sites.append((decoded, args, node.start_point[0] + 1))
            stack.extend(node.children)
        return sites


def _text(node: TSNode, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _classify_dotted_call(dotted: str) -> str | None:
    """Match a dotted callee (``requests.get``, ``p.write_text``) to a tag."""
    if any(ch in dotted for ch in "()[] \n"):
        # A computed receiver (call/subscript chain) — skip rather than guess.
        return None
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[-1] in _DB_METHODS and parts[-2] in _DB_RECEIVERS:
        return "db"
    if dotted in _IO_DOTTED_EXACT:
        return "io"
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


def _arg_names(args_node: TSNode, source: bytes) -> list[str]:
    """Data identifiers read inside a call's argument list.

    Attribute names and nested callee names are excluded — only names that
    carry data count (mirrors the CFG ``reads`` rules).
    """
    names: list[str] = []
    seen: set[str] = set()

    def collect(node: TSNode) -> None:
        if node.type == "identifier":
            text = _text(node, source)
            if text not in seen:
                seen.add(text)
                names.append(text)
            return
        if node.type == "attribute":
            obj = node.child_by_field_name("object")
            if obj is not None:
                collect(obj)
            return
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "attribute":
                obj = fn.child_by_field_name("object")
                if obj is not None:
                    collect(obj)
            inner = node.child_by_field_name("arguments")
            if inner is not None:
                collect(inner)
            return
        for child in node.children:
            collect(child)

    collect(args_node)
    return names
