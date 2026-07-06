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

from cgir.languages.base import (
    AssignDesc,
    BranchDesc,
    CallSite,
    CaseDesc,
    HandlerDesc,
    LanguageAdapter,
    LoopDesc,
    MatchDesc,
    ReturnDesc,
    SimpleDesc,
    StatementDesc,
    TryDesc,
    WithDesc,
)

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

    # --- phase 2: CFG extraction --------------------------------------------

    def function_body(self, func_node: TSNode) -> TSNode | None:
        return func_node.child_by_field_name("body")

    def block_statements(self, block: TSNode) -> list[TSNode]:
        return [c for c in block.named_children if c.type != "comment"]

    def describe_statement(self, node: TSNode, source: bytes) -> StatementDesc:
        t = node.type
        if t in {"if_statement", "elif_clause"}:
            return self._describe_branch(node, source)
        if t == "for_statement":
            left = node.child_by_field_name("left")
            writes, _ = _split_pattern(left, source) if left is not None else ([], [])
            return LoopDesc(
                reads=_extract_reads(node, source),
                writes=writes,
                body=node.child_by_field_name("body"),
            )
        if t == "while_statement":
            return LoopDesc(
                reads=_extract_reads(node, source),
                writes=[],
                body=node.child_by_field_name("body"),
            )
        if t == "return_statement":
            return ReturnDesc(
                reads=_extract_reads(node, source),
                mutates=_extract_call_mutations(node, source),
            )
        if t == "with_statement":
            writes, reads = _with_targets(node, source)
            return WithDesc(writes=writes, reads=reads, body=node.child_by_field_name("body"))
        if t == "try_statement":
            return self._describe_try(node, source)
        if t == "match_statement":
            desc = self._describe_match(node, source)
            if desc.cases:
                return desc
            # No cases: degrade to an opaque statement.
            return SimpleDesc(
                reads=_extract_reads(node, source),
                mutates=_extract_call_mutations(node, source),
            )
        if t == "expression_statement" and _is_assignment(node):
            writes, mutates = _extract_lhs_targets(node, source)
            for base in _extract_call_mutations(node, source):
                if base not in mutates:
                    mutates.append(base)
            return AssignDesc(writes=writes, mutates=mutates, reads=_extract_reads(node, source))
        return SimpleDesc(
            reads=_extract_reads(node, source),
            mutates=_extract_call_mutations(node, source),
        )

    def _describe_branch(self, node: TSNode, source: bytes) -> BranchDesc:
        else_block: TSNode | None = None
        next_branch: TSNode | None = None
        alternative = node.child_by_field_name("alternative")
        if alternative is not None:
            if alternative.type == "else_clause":
                else_block = alternative.child_by_field_name("body")
            elif alternative.type == "elif_clause":
                next_branch = alternative
        return BranchDesc(
            reads=_extract_reads(node, source),
            consequence=node.child_by_field_name("consequence"),
            else_block=else_block,
            next_branch=next_branch,
        )

    def _describe_try(self, node: TSNode, source: bytes) -> TryDesc:
        handlers: list[HandlerDesc] = []
        else_block: TSNode | None = None
        finally_block: TSNode | None = None
        for child in node.named_children:
            if child.type == "except_clause":
                writes: list[str] = []
                value = child.child_by_field_name("value")
                if value is not None and value.type == "as_pattern":
                    alias = value.child_by_field_name("alias")
                    if alias is not None and alias.named_children:
                        writes, _ = _split_pattern(alias.named_children[0], source)
                block = next((c for c in child.children if c.type == "block"), None)
                handlers.append(HandlerDesc(node=child, writes=writes, block=block))
            elif child.type == "else_clause":
                else_block = child.child_by_field_name("body")
            elif child.type == "finally_clause":
                finally_block = next((c for c in child.children if c.type == "block"), None)
        return TryDesc(
            body=node.child_by_field_name("body"),
            handlers=handlers,
            else_block=else_block,
            finally_block=finally_block,
        )

    def _describe_match(self, node: TSNode, source: bytes) -> MatchDesc:
        subject = node.child_by_field_name("subject")
        subject_reads: list[str] = []
        if subject is not None:
            seen: set[str] = set()
            _collect_reads(subject, source, subject_reads, seen)
        body = node.child_by_field_name("body")
        cases: list[CaseDesc] = []
        if body is not None:
            for case in body.named_children:
                if case.type != "case_clause":
                    continue
                reads = list(subject_reads)
                guard = case.child_by_field_name("guard")
                if guard is not None:
                    seen_g: set[str] = set(reads)
                    _collect_reads(guard, source, reads, seen_g)
                cases.append(
                    CaseDesc(
                        node=case,
                        reads=reads,
                        consequence=case.child_by_field_name("consequence"),
                    )
                )
        return MatchDesc(cases=cases)

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


# --- CFG statement extraction (moved from analyses/cfg.py in phase 2) ----------

_ASSIGNMENT_TYPES: frozenset[str] = frozenset({"assignment", "augmented_assignment"})

_MUTATOR_METHODS: frozenset[str] = frozenset(
    {
        "add",
        "append",
        "appendleft",
        "clear",
        "discard",
        "extend",
        "extendleft",
        "insert",
        "pop",
        "popitem",
        "popleft",
        "put",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
        "write",
        "writelines",
        # DB-session style mutators (SQLAlchemy et al.) — found on real repos
        # where `db.delete(ch)` classified pure while `db.add(ch)` didn't.
        "delete",
        "commit",
        "rollback",
        "flush",
        "merge",
        "expunge",
    }
)


def _is_assignment(expr_stmt: TSNode) -> bool:
    return any(child.type in _ASSIGNMENT_TYPES for child in expr_stmt.children)


def _extract_lhs_targets(expr_stmt: TSNode, source: bytes) -> tuple[list[str], list[str]]:
    """Split an assignment's LHS into (writes, mutates).

    ``self.x = 1`` records ``mutates=["self"]``; ``xs[0] = 1`` records
    ``mutates=["xs"]``; ``x, obj.y = ...`` records ``writes=["x"]`` and
    ``mutates=["obj"]``. Augmented assignments follow the same LHS rules.
    """
    for child in expr_stmt.children:
        if child.type in _ASSIGNMENT_TYPES:
            left = child.child_by_field_name("left")
            if left is not None:
                return _split_pattern(left, source)
    return [], []


def _split_pattern(ts_node: TSNode, source: bytes) -> tuple[list[str], list[str]]:
    if ts_node.type == "identifier":
        return [_text(ts_node, source)], []
    if ts_node.type == "attribute":
        obj = ts_node.child_by_field_name("object")
        return [], _base_names(obj, source) if obj is not None else []
    if ts_node.type == "subscript":
        base = ts_node.child_by_field_name("value")
        return [], _base_names(base, source) if base is not None else []
    if ts_node.type in {"tuple_pattern", "list_pattern", "pattern_list"}:
        writes: list[str] = []
        mutates: list[str] = []
        for child in ts_node.named_children:
            w, m = _split_pattern(child, source)
            writes.extend(w)
            mutates.extend(m)
        return writes, mutates
    return [], []


def _base_names(ts_node: TSNode, source: bytes) -> list[str]:
    """For ``a.b.c`` return ``['a']``; for ``xs`` return ``['xs']``."""
    cur = ts_node
    while cur.type in {"attribute", "subscript"}:
        nxt = cur.child_by_field_name("object" if cur.type == "attribute" else "value")
        if nxt is None:
            return []
        cur = nxt
    if cur.type == "identifier":
        return [_text(cur, source)]
    return []


def _extract_reads(stmt_ts: TSNode, source: bytes) -> list[str]:
    """Identifier names read as data by this statement.

    Per stmt kind we pick the right sub-expression (RHS / condition /
    iterable / returned value / generic). Attribute names and called
    function names are excluded — only data identifiers count.
    """
    names: list[str] = []
    seen: set[str] = set()
    if stmt_ts.type == "expression_statement":
        aug = next((c for c in stmt_ts.children if c.type == "augmented_assignment"), None)
        if aug is not None:
            left = aug.child_by_field_name("left")
            right = aug.child_by_field_name("right")
            if left is not None:
                _collect_reads(left, source, names, seen)
            if right is not None:
                _collect_reads(right, source, names, seen)
            return names
    target = _read_target(stmt_ts)
    if target is None:
        return []
    _collect_reads(target, source, names, seen)
    return names


def _with_targets(with_ts: TSNode, source: bytes) -> tuple[list[str], list[str]]:
    """(writes, reads) for a ``with`` header."""
    writes: list[str] = []
    reads: list[str] = []
    seen: set[str] = set()
    clause = next((c for c in with_ts.children if c.type == "with_clause"), None)
    if clause is None:
        return writes, reads
    for item in clause.named_children:
        if item.type != "with_item":
            continue
        value = item.child_by_field_name("value")
        if value is None:
            continue
        if value.type == "as_pattern":
            context = value.named_children[0] if value.named_children else None
            alias = value.child_by_field_name("alias")
            if context is not None:
                _collect_reads(context, source, reads, seen)
            if alias is not None and alias.named_children:
                w, _ = _split_pattern(alias.named_children[0], source)
                writes.extend(w)
        else:
            _collect_reads(value, source, reads, seen)
    return writes, reads


def _extract_call_mutations(stmt_ts: TSNode, source: bytes) -> list[str]:
    """Receiver base names mutated by mutator method calls in this statement.

    Walks the whole statement subtree, so ``xs.append(x)``, assignment RHS
    (``x = xs.pop()``), and return expressions all count.
    """
    names: list[str] = []
    stack: list[TSNode] = [stmt_ts]
    while stack:
        node = stack.pop()
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                obj = fn.child_by_field_name("object")
                if attr is not None and obj is not None and _text(attr, source) in _MUTATOR_METHODS:
                    for base in _base_names(obj, source):
                        if base not in names:
                            names.append(base)
        stack.extend(node.children)
    return names


def _read_target(ts_node: TSNode) -> TSNode | None:
    if ts_node.type == "expression_statement":
        for child in ts_node.children:
            if child.type == "assignment":
                # For assignments, only the RHS contributes data reads. The
                # LHS base (in attribute/subscript) is handled via `mutates`.
                return child.child_by_field_name("right")
        return ts_node
    if ts_node.type == "return_statement":
        for child in ts_node.named_children:
            return child
        return None
    if ts_node.type in {"if_statement", "elif_clause", "while_statement"}:
        return ts_node.child_by_field_name("condition")
    if ts_node.type == "for_statement":
        return ts_node.child_by_field_name("right")
    return ts_node


def _collect_reads(ts_node: TSNode, source: bytes, names: list[str], seen: set[str]) -> None:
    if ts_node.type == "identifier":
        name = _text(ts_node, source)
        if name not in seen:
            seen.add(name)
            names.append(name)
        return
    if ts_node.type == "attribute":
        obj = ts_node.child_by_field_name("object")
        if obj is not None:
            _collect_reads(obj, source, names, seen)
        return  # skip the attribute identifier
    if ts_node.type == "call":
        fn = ts_node.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            if obj is not None:
                _collect_reads(obj, source, names, seen)
        # else: a bare-identifier callee is not a data read; skip.
        args = ts_node.child_by_field_name("arguments")
        if args is not None:
            _collect_reads(args, source, names, seen)
        return
    for child in ts_node.children:
        _collect_reads(child, source, names, seen)
