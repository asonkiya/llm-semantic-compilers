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
    ClassDecl,
    Declaration,
    FunctionDecl,
    HandlerDesc,
    ImportDecl,
    LanguageAdapter,
    LoopDesc,
    MatchDesc,
    ParamDecl,
    PinIndex,
    ReturnDesc,
    SimpleDesc,
    StatementDesc,
    TryDesc,
    VariableDecl,
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

    # --- phase 3: ingest extraction ------------------------------------------

    def module_declarations(
        self, root: TSNode, source: bytes, module_name: str, rel_path: str
    ) -> list[Declaration]:
        # Python resolves relative imports against the dotted module name;
        # rel_path is unused.
        pin_index = PinIndex(root, source)
        decls: list[Declaration] = []
        for child in root.children:
            decls.extend(self._top_level_decl(child, source, module_name, pin_index))
        first_decl = next((c for c in root.children if c.type != "comment"), None)
        # A header block only belongs to the first statement when that
        # statement is *pinnable* (a definition) — touching an import keeps
        # the pins module-level.
        pinnable = {"function_definition", "class_definition", "decorated_definition"}
        module_pins = pin_index.module_pins(
            first_decl.start_point[0]
            if first_decl is not None and first_decl.type in pinnable
            else None
        )
        if module_pins:
            for decl in decls:
                if isinstance(decl, FunctionDecl):
                    decl.pins = sorted(set(decl.pins) | set(module_pins))
                elif isinstance(decl, ClassDecl):
                    for method in decl.methods:
                        method.pins = sorted(set(method.pins) | set(module_pins))
        return decls

    def _top_level_decl(
        self, node: TSNode, source: bytes, module_name: str, pin_index: PinIndex
    ) -> list[Declaration]:
        t = node.type
        if t == "function_definition":
            return [
                self._function_decl(
                    node, source, [], is_method=False, pins=pin_index.for_definition(node)
                )
            ]
        if t == "class_definition":
            return [self._class_decl(node, source, pin_index)]
        if t == "decorated_definition":
            inner = _undecorated(node)
            if inner is None:
                return []
            decorators = _decorator_texts(node, source)
            pins = pin_index.for_definition(node)  # outermost: pin above the decorator
            if inner.type == "function_definition":
                return [self._function_decl(inner, source, decorators, is_method=False, pins=pins)]
            if inner.type == "class_definition":
                return [self._class_decl(inner, source, pin_index)]
            return []
        if t in {"import_statement", "import_from_statement"}:
            return [
                ImportDecl(node=node, target=target, alias=alias)
                for target, alias in _import_targets(node, source, module_name)
            ]
        if t == "expression_statement":
            assign = next((c for c in node.children if c.type == "assignment"), None)
            if assign is None:
                return []
            left = assign.child_by_field_name("left")
            if left is None:
                return []
            return [
                VariableDecl(node=node, name=name)
                for name in _assignment_target_names(left, source)
            ]
        return []

    def _class_decl(self, node: TSNode, source: bytes, pin_index: PinIndex) -> ClassDecl:
        name = _identifier_text(node.child_by_field_name("name"), source) or "<anonymous>"
        methods: list[FunctionDecl] = []
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                if child.type == "function_definition":
                    methods.append(
                        self._function_decl(
                            child,
                            source,
                            [],
                            is_method=True,
                            pins=pin_index.for_definition(child),
                        )
                    )
                elif child.type == "decorated_definition":
                    inner = _undecorated(child)
                    if inner is not None and inner.type == "function_definition":
                        methods.append(
                            self._function_decl(
                                inner,
                                source,
                                _decorator_texts(child, source),
                                is_method=True,
                                pins=pin_index.for_definition(child),
                            )
                        )
        fields = _class_fields(body, source) if body is not None else {}
        return ClassDecl(node=node, name=name, methods=methods, fields=fields)

    def _function_decl(
        self,
        node: TSNode,
        source: bytes,
        decorators: list[str],
        is_method: bool,
        pins: list[str] | None = None,
    ) -> FunctionDecl:
        name = _identifier_text(node.child_by_field_name("name"), source) or "<anonymous>"
        params: list[ParamDecl] = []
        params_node = node.child_by_field_name("parameters")
        if params_node is not None:
            for child in params_node.children:
                param_name = _param_name(child, source)
                if param_name is None:
                    continue
                if is_method and param_name == "self":
                    continue
                params.append(ParamDecl(name=param_name, node=child))
        return FunctionDecl(
            node=node,
            name=name,
            params=params,
            signature=_signature_text(node, source),
            returns=_return_annotation_text(node, source),
            doc=_docstring_text(node, source),
            raises=_raised_names(node, source),
            decorators=list(decorators),
            free_names=_free_names(node, source),
            pins=list(pins or []),
        )

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


# --- module/ingest extraction (moved from sources/tree_sitter_source.py) -------

_PY_BUILTINS: frozenset[str] = frozenset(
    {
        "True",
        "False",
        "None",
        "self",
        "cls",
        "print",
        "len",
        "range",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sorted",
        "list",
        "dict",
        "set",
        "tuple",
        "frozenset",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "type",
        "isinstance",
        "issubclass",
        "hasattr",
        "getattr",
        "setattr",
        "min",
        "max",
        "sum",
        "abs",
        "round",
        "any",
        "all",
        "next",
        "iter",
        "open",
        "super",
        "object",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "RuntimeError",
        "StopIteration",
        "repr",
        "format",
        "vars",
        "dir",
        "id",
        "input",
        "reversed",
        "slice",
        "property",
        "staticmethod",
        "classmethod",
    }
)


def _type_base(type_node: TSNode | None, source: bytes) -> str | None:
    """Base class name of an annotation: ``Svc`` from ``Svc``/``Svc[T]``.

    Dotted annotations (``mod.Svc``) are skipped — they resolve through a
    module binding, not a plain name, and guessing the tail risks false
    positives in field-call resolution.
    """
    if type_node is None:
        return None
    node = type_node
    if node.type == "type" and node.named_child_count:
        node = node.named_children[0]
    if node.type == "identifier":
        return _text(node, source)
    if node.type == "generic_type" or node.type == "subscript":
        first = node.named_children[0] if node.named_child_count else None
        if first is not None and first.type == "identifier":
            return _text(first, source)
    return None


def _class_fields(body: TSNode, source: bytes) -> dict[str, str]:
    """Field name -> declared type, from the DI-relevant idioms.

    Class-level annotations (``svc: Svc``), ``__init__`` params stored on
    ``self`` (``self.svc = svc`` where ``svc`` is annotated), and direct
    construction (``self.svc = Svc()``). Feeds ``self.<field>.<method>()``
    call resolution — the Python analog of TS constructor DI.
    """
    fields: dict[str, str] = {}
    for child in body.children:
        if child.type == "expression_statement" and child.child_count:
            assign = child.children[0]
            if assign.type != "assignment":
                continue
            left = assign.child_by_field_name("left")
            base = _type_base(assign.child_by_field_name("type"), source)
            if left is not None and left.type == "identifier" and base:
                fields[_text(left, source)] = base
            continue
        init = child if child.type == "function_definition" else _undecorated(child)
        if (
            init is not None
            and init.type == "function_definition"
            and _identifier_text(init.child_by_field_name("name"), source) == "__init__"
        ):
            fields.update(_init_fields(init, source))
    return fields


def _init_fields(init: TSNode, source: bytes) -> dict[str, str]:
    param_types: dict[str, str] = {}
    params = init.child_by_field_name("parameters")
    for param in params.children if params is not None else []:
        if param.type in ("typed_parameter", "typed_default_parameter"):
            name_node = next((c for c in param.children if c.type == "identifier"), None)
            base = _type_base(param.child_by_field_name("type"), source)
            if name_node is not None and base:
                param_types[_text(name_node, source)] = base

    fields: dict[str, str] = {}
    stack = [init.child_by_field_name("body")]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if (
                left is not None
                and left.type == "attribute"
                and right is not None
                and _identifier_text(left.child_by_field_name("object"), source) == "self"
            ):
                attr = _identifier_text(left.child_by_field_name("attribute"), source)
                if attr:
                    if right.type == "identifier" and _text(right, source) in param_types:
                        fields[attr] = param_types[_text(right, source)]
                    elif right.type == "call":
                        callee = right.child_by_field_name("function")
                        if callee is not None and callee.type == "identifier":
                            fields[attr] = _text(callee, source)
        stack.extend(node.children)
    return fields


def _undecorated(decorated_ts: TSNode) -> TSNode | None:
    """Return the function/class wrapped by a ``decorated_definition``."""
    for child in decorated_ts.named_children:
        if child.type in {"function_definition", "class_definition"}:
            return child
    return None


def _decorator_texts(decorated_ts: TSNode, source: bytes) -> list[str]:
    """Each decorator's call text, without the leading ``@``."""
    texts: list[str] = []
    for child in decorated_ts.named_children:
        if child.type == "decorator":
            raw = source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
            texts.append(raw.lstrip("@").strip())
    return texts


def _identifier_text(node: TSNode | None, source: bytes) -> str | None:
    if node is None:
        return None
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _signature_text(func_node: TSNode, source: bytes) -> str:
    name = _identifier_text(func_node.child_by_field_name("name"), source) or ""
    params_node = func_node.child_by_field_name("parameters")
    params_text = _identifier_text(params_node, source) or "()"
    return_node = func_node.child_by_field_name("return_type")
    return_text = _identifier_text(return_node, source)
    sig = f"{name}{params_text}"
    if return_text:
        sig += f" -> {return_text}"
    return sig


def _return_annotation_text(func_node: TSNode, source: bytes) -> str | None:
    """The declared return type (``-> float`` gives ``"float"``), if any."""
    return _identifier_text(func_node.child_by_field_name("return_type"), source)


def _docstring_text(func_node: TSNode, source: bytes) -> str:
    """The function's docstring (first string statement in the body), cleaned."""
    body = func_node.child_by_field_name("body")
    if body is None:
        return ""
    for stmt in body.named_children:
        if stmt.type == "comment":
            continue
        if stmt.type == "expression_statement" and stmt.named_children:
            inner = stmt.named_children[0]
            if inner.type == "string":
                raw = source[inner.start_byte : inner.end_byte].decode("utf-8", errors="replace")
                return _clean_docstring(raw)
        return ""  # first real statement isn't a string -> no docstring
    return ""


def _clean_docstring(raw: str) -> str:
    text = raw.strip()
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote):
            text = text[len(quote) :]
            if text.endswith(quote):
                text = text[: -len(quote)]
            break
    return text.strip()


def _raised_names(func_node: TSNode, source: bytes) -> list[str]:
    """Exception class names raised in the body."""
    body = func_node.child_by_field_name("body")
    if body is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "raise_statement":
            for child in node.named_children:
                target = child.child_by_field_name("function") if child.type == "call" else child
                if target is None:
                    continue
                text = _text(target, source)
                name = text.split(".")[-1].split("(")[0].strip()
                if name and name[0].isupper() and name not in seen:
                    seen.add(name)
                    names.append(name)
                break
        stack.extend(node.children)
    return names


def _free_names(func_node: TSNode, source: bytes) -> list[str]:
    """Names referenced in the body that are not params, locals, or builtins."""
    body = func_node.child_by_field_name("body")
    if body is None:
        return []
    bound: set[str] = set()
    params = func_node.child_by_field_name("parameters")
    if params is not None:
        for child in params.children:
            name = _param_name(child, source)
            if name:
                bound.add(name)
    _collect_bound(body, source, bound)

    referenced: list[str] = []
    seen: set[str] = set()
    _collect_referenced(body, source, referenced, seen)
    return [n for n in referenced if n not in bound and n not in _PY_BUILTINS]


def _collect_bound(node: TSNode, source: bytes, bound: set[str]) -> None:
    t = node.type
    if t in {"assignment", "augmented_assignment"} or t == "for_statement":
        left = node.child_by_field_name("left")
        if left is not None:
            bound.update(_assignment_target_names(left, source))
    elif t in {"function_definition", "class_definition"}:
        def_name = _identifier_text(node.child_by_field_name("name"), source)
        if def_name:
            bound.add(def_name)
    elif t == "as_pattern":
        alias_node = node.child_by_field_name("alias")
        if alias_node is not None:
            for ident in _all_identifiers(alias_node, source):
                bound.add(ident)
    elif t == "except_clause":
        for child in node.children:
            if child.type == "identifier":
                bound.add(_text(child, source))
    elif t == "named_expression":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            bound.add(_text(name_node, source))
    elif t in {"import_statement", "import_from_statement"}:
        for target, alias in _import_targets(node, source, ""):
            bound.add(alias or target.rsplit(".", 1)[-1])
    for child in node.children:
        _collect_bound(child, source, bound)


def _collect_referenced(node: TSNode, source: bytes, out: list[str], seen: set[str]) -> None:
    t = node.type
    if t == "identifier":
        name = _text(node, source)
        if name not in seen:
            seen.add(name)
            out.append(name)
        return
    if t == "attribute":
        obj = node.child_by_field_name("object")
        if obj is not None:
            _collect_referenced(obj, source, out, seen)
        return  # skip the attribute suffix name
    if t == "keyword_argument":
        value = node.child_by_field_name("value")
        if value is not None:
            _collect_referenced(value, source, out, seen)
        return  # skip the keyword name
    if t in {"import_statement", "import_from_statement"}:
        return
    for child in node.children:
        _collect_referenced(child, source, out, seen)


def _all_identifiers(node: TSNode, source: bytes) -> list[str]:
    out: list[str] = []
    if node.type == "identifier":
        return [_text(node, source)]
    for child in node.children:
        out.extend(_all_identifiers(child, source))
    return out


def _param_name(node: TSNode, source: bytes) -> str | None:
    if node.type == "identifier":
        return _identifier_text(node, source)
    if node.type in {
        "typed_parameter",
        "default_parameter",
        "typed_default_parameter",
        "list_splat_pattern",
        "dictionary_splat_pattern",
    }:
        # Search children for the identifier that names the parameter.
        for child in node.children:
            if child.type == "identifier":
                return _identifier_text(child, source)
    return None


def _assignment_target_names(left: TSNode, source: bytes) -> list[str]:
    """Bound names on an assignment LHS: ``x``, ``x, y``; attribute/subscript LHS none."""
    if left.type == "identifier":
        text = _identifier_text(left, source)
        return [text] if text else []
    if left.type in {"pattern_list", "tuple_pattern", "list_pattern"}:
        names: list[str] = []
        for child in left.named_children:
            names.extend(_assignment_target_names(child, source))
        return names
    return []


def _import_targets(
    node: TSNode, source: bytes, current_module: str
) -> list[tuple[str, str | None]]:
    """Yield ``(absolute_target, local_alias_or_None)`` per imported name."""
    targets: list[tuple[str, str | None]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type in {"dotted_name", "aliased_import"}:
                name = _identifier_text(_name_child(child), source)
                if name:
                    targets.append((name, _alias_text(child, source)))
    elif node.type == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        module = _resolve_from_module(module_node, source, current_module) if module_node else ""
        for child in node.children_by_field_name("name"):
            name = _identifier_text(_name_child(child), source)
            if name:
                target = f"{module}.{name}" if module else name
                targets.append((target, _alias_text(child, source)))
    return targets


def _alias_text(ts_node: TSNode, source: bytes) -> str | None:
    """The ``as`` alias of an ``aliased_import``, if any."""
    if ts_node.type != "aliased_import":
        return None
    return _identifier_text(ts_node.child_by_field_name("alias"), source)


def _resolve_from_module(module_node: TSNode, source: bytes, current_module: str) -> str:
    """Absolute dotted name of an ``import_from_statement`` module (incl. relative)."""
    if module_node.type == "relative_import":
        dots = 0
        sub_name = ""
        for child in module_node.children:
            if child.type == "import_prefix":
                # import_prefix's text is one or more "." characters.
                raw = source[child.start_byte : child.end_byte]
                dots = raw.count(b".")
            elif child.type == "dotted_name":
                sub_name = _identifier_text(child, source) or ""
        if dots == 0:
            return sub_name
        parts = current_module.split(".")
        # Drop the module itself; each extra dot peels off one more package level.
        package_parts = parts[:-1]
        up = dots - 1
        if up > 0:
            package_parts = package_parts[:-up] if up <= len(package_parts) else []
        absolute = list(package_parts)
        if sub_name:
            absolute.extend(sub_name.split("."))
        return ".".join(absolute)
    return _identifier_text(module_node, source) or ""


def _name_child(node: TSNode) -> TSNode:
    if node.type == "aliased_import":
        sub = node.child_by_field_name("name")
        if sub is not None:
            return sub
    return node
