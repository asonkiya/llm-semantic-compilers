"""TypeScriptAdapter — the TypeScript implementation of :class:`LanguageAdapter`.

Maps the tree-sitter-typescript grammar to CGIR's normalized descriptors.
Covers function/arrow/method declarations, classes, ES module imports
(with relative-specifier resolution), the common statement forms
(if/for-of/for/while/return/try/switch/throw + const/let/var + assignment),
call sites, and a JS/TS-flavoured effect table.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

import tree_sitter_typescript as tsts
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
)

# --- effect tables (JS/TS runtime + common libs) ------------------------------

_IO_PREFIXES: tuple[str, ...] = ("console.",)
_NET_EXACT: frozenset[str] = frozenset({"fetch"})
_NET_PREFIXES: tuple[str, ...] = ("axios.", "http.", "https.", "got.", "superagent.")
# HTTP-client method calls: `<receiver>.get(...)` where the receiver names an
# HTTP client (Angular DI: `this.http.get`, `this.httpClient.post`).
_NET_RECEIVERS: frozenset[str] = frozenset({"http", "https", "httpClient", "httpclient", "$http"})
_HTTP_VERBS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "request"}
)
_FS_PREFIXES: tuple[str, ...] = ("fs.", "fsPromises.", "fs.promises.")
_FS_METHOD_SUFFIXES: tuple[str, ...] = (
    ".readFile",
    ".writeFile",
    ".readFileSync",
    ".writeFileSync",
    ".appendFile",
    ".unlink",
    ".mkdir",
    ".rmdir",
    ".rm",
)
_NONDETERM_EXACT: frozenset[str] = frozenset(
    {"Math.random", "Date.now", "crypto.randomUUID", "crypto.randomBytes", "performance.now"}
)
_DB_RECEIVERS: frozenset[str] = frozenset(
    {
        "db",
        "database",
        "pool",
        "client",
        "conn",
        "connection",
        "knex",
        "prisma",
        "repo",
        "repository",
        "queryRunner",
    }
)
_DB_METHODS: frozenset[str] = frozenset(
    {
        "query",
        "execute",
        "insert",
        "update",
        "delete",
        "save",
        "find",
        "findOne",
        "findMany",
        "create",
        "commit",
        "rollback",
        "transaction",
        "raw",
    }
)

_MUTATOR_METHODS: frozenset[str] = frozenset(
    {
        "push",
        "pop",
        "shift",
        "unshift",
        "splice",
        "sort",
        "reverse",
        "fill",
        "copyWithin",
        "set",
        "add",
        "delete",
        "clear",
    }
)

_JS_GLOBALS: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "null",
        "undefined",
        "this",
        "super",
        "console",
        "Math",
        "Date",
        "JSON",
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Promise",
        "Error",
        "Map",
        "Set",
        "Symbol",
        "RegExp",
        "parseInt",
        "parseFloat",
        "isNaN",
        "Infinity",
        "NaN",
        "require",
        "module",
        "exports",
        "process",
        "window",
        "document",
        "globalThis",
        "Reflect",
        "Proxy",
        "WeakMap",
        "WeakSet",
    }
)

_FUNCTION_VALUES: frozenset[str] = frozenset({"arrow_function", "function_expression", "function"})


class TypeScriptAdapter(LanguageAdapter):
    name = "typescript"
    file_extensions = (".ts", ".tsx")

    def __init__(self) -> None:
        self._parser = Parser(Language(tsts.language_typescript()))

    def parse(self, source: bytes) -> TSNode:
        return self._parser.parse(source).root_node

    # --- location ------------------------------------------------------------

    def locate_function(self, root: TSNode, name: str, start_row: int) -> TSNode | None:
        stack: list[TSNode] = [root]
        while stack:
            node = stack.pop()
            if node.start_point[0] == start_row:
                if node.type in {
                    "function_declaration",
                    "method_definition",
                    "function_expression",
                }:
                    if _node_name(node) == name:
                        return node
                elif node.type in {"lexical_declaration", "variable_declaration"}:
                    # arrow/function assigned to const/let — return the value node
                    # (which carries the body) so downstream passes see the function.
                    for decl in node.named_children:
                        if decl.type != "variable_declarator":
                            continue
                        nn = decl.child_by_field_name("name")
                        val = decl.child_by_field_name("value")
                        if (
                            nn is not None
                            and _node_text(nn) == name
                            and val is not None
                            and val.type in _FUNCTION_VALUES
                        ):
                            return val
            stack.extend(node.children)
        return None

    # --- effects -------------------------------------------------------------

    def direct_effects(self, func_node: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
        tags: set[str] = set()
        body = self.function_body(func_node)
        if body is None:
            return tags
        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()
            if node.type == "throw_statement":
                tags.add("raise")
            elif node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    dotted = _text(fn, source)
                    head, _, rest = dotted.partition(".")
                    if rest and head in aliases and aliases[head] != head:
                        dotted = f"{aliases[head]}.{rest}"
                    elif dotted in aliases:
                        dotted = aliases[dotted]
                    tag = _classify_call(dotted)
                    if tag is not None:
                        tags.add(tag)
            stack.extend(node.children)
        return tags

    def call_sites(self, func_node: TSNode, source: bytes) -> list[CallSite]:
        sites: list[CallSite] = []
        body = self.function_body(func_node)
        if body is None:
            return sites
        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None and fn.type in {"identifier", "member_expression"}:
                    decoded = _text(fn, source)
                    if "(" in decoded or "[" in decoded or "\n" in decoded:
                        decoded = decoded.split(".", 1)[0]
                    args = node.child_by_field_name("arguments")
                    names = _arg_names(args, source) if args is not None else []
                    sites.append((decoded, names, node.start_point[0] + 1))
            stack.extend(node.children)
        return sites

    # --- CFG -----------------------------------------------------------------

    def function_body(self, func_node: TSNode) -> TSNode | None:
        body = func_node.child_by_field_name("body")
        # arrow with an expression body has no block → no CFG
        return body if body is not None and body.type == "statement_block" else None

    def block_statements(self, block: TSNode) -> list[TSNode]:
        if block.type in {"switch_case", "switch_default"}:
            return [
                c
                for c in block.children_by_field_name("body")
                if c.type not in {"break_statement", "comment"}
            ]
        return [c for c in block.named_children if c.type != "comment"]

    def describe_statement(self, node: TSNode, source: bytes) -> StatementDesc:
        t = node.type
        if t == "if_statement":
            return self._describe_if(node, source)
        if t == "for_in_statement":  # for-of / for-in
            left = node.child_by_field_name("left")
            writes = _target_names(left, source) if left is not None else []
            return LoopDesc(
                reads=_reads_of(node.child_by_field_name("right"), source),
                writes=writes,
                body=node.child_by_field_name("body"),
            )
        if t == "for_statement":
            return LoopDesc(
                reads=_reads_of(node.child_by_field_name("condition"), source),
                writes=[],
                body=node.child_by_field_name("body"),
            )
        if t == "while_statement":
            return LoopDesc(
                reads=_reads_of(node.child_by_field_name("condition"), source),
                writes=[],
                body=node.child_by_field_name("body"),
            )
        if t == "return_statement":
            expr = next((c for c in node.named_children), None)
            return ReturnDesc(reads=_reads_of(expr, source), mutates=_call_mutations(node, source))
        if t == "try_statement":
            return self._describe_try(node)
        if t == "switch_statement":
            desc = self._describe_switch(node, source)
            if desc.cases:
                return desc
            return SimpleDesc(reads=_reads_of(node, source), mutates=_call_mutations(node, source))
        if t in {"lexical_declaration", "variable_declaration"}:
            return self._describe_declaration(node, source)
        if t == "expression_statement":
            inner = next((c for c in node.named_children), None)
            if inner is not None and inner.type == "assignment_expression":
                left = inner.child_by_field_name("left")
                right = inner.child_by_field_name("right")
                writes, mutates = _lhs_split(left, source) if left is not None else ([], [])
                for base in _call_mutations(node, source):
                    if base not in mutates:
                        mutates.append(base)
                return AssignDesc(writes=writes, mutates=mutates, reads=_reads_of(right, source))
            return SimpleDesc(reads=_reads_of(node, source), mutates=_call_mutations(node, source))
        return SimpleDesc(reads=_reads_of(node, source), mutates=_call_mutations(node, source))

    def _describe_if(self, node: TSNode, source: bytes) -> BranchDesc:
        else_block: TSNode | None = None
        next_branch: TSNode | None = None
        alt = node.child_by_field_name("alternative")
        if alt is not None and alt.type == "else_clause":
            inner = next((c for c in alt.named_children), None)
            if inner is not None and inner.type == "if_statement":
                next_branch = inner
            else:
                else_block = inner
        return BranchDesc(
            reads=_reads_of(node.child_by_field_name("condition"), source),
            consequence=node.child_by_field_name("consequence"),
            else_block=else_block,
            next_branch=next_branch,
        )

    def _describe_declaration(self, node: TSNode, source: bytes) -> AssignDesc:
        writes: list[str] = []
        reads: list[str] = []
        mutates: list[str] = []
        seen: set[str] = set()
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name = decl.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                writes.append(_text(name, source))
            value = decl.child_by_field_name("value")
            if value is not None:
                _collect_reads(value, source, reads, seen)
        for base in _call_mutations(node, source):
            if base not in mutates:
                mutates.append(base)
        return AssignDesc(writes=writes, mutates=mutates, reads=reads)

    def _describe_try(self, node: TSNode) -> TryDesc:
        handlers: list[HandlerDesc] = []
        handler = node.child_by_field_name("handler")
        if handler is not None:
            param = handler.child_by_field_name("parameter")
            writes = [param.text.decode()] if param is not None and param.text else []
            handlers.append(
                HandlerDesc(node=handler, writes=writes, block=handler.child_by_field_name("body"))
            )
        finalizer = node.child_by_field_name("finalizer")
        finally_block = finalizer.child_by_field_name("body") if finalizer is not None else None
        return TryDesc(
            body=node.child_by_field_name("body"),
            handlers=handlers,
            else_block=None,
            finally_block=finally_block,
        )

    def _describe_switch(self, node: TSNode, source: bytes) -> MatchDesc:
        value = node.child_by_field_name("value")
        subject_reads = _reads_of(value, source)
        body = node.child_by_field_name("body")
        cases: list[CaseDesc] = []
        if body is not None:
            for case in body.named_children:
                if case.type not in {"switch_case", "switch_default"}:
                    continue
                cases.append(CaseDesc(node=case, reads=list(subject_reads), consequence=case))
        return MatchDesc(cases=cases)

    # --- ingest --------------------------------------------------------------

    def module_declarations(
        self, root: TSNode, source: bytes, module_name: str, rel_path: str
    ) -> list[Declaration]:
        pin_index = PinIndex(root, source)
        decls: list[Declaration] = []
        for child in root.children:
            decls.extend(self._top_level(child, source, rel_path, pin_index))
        first_decl = next((c for c in root.children if c.type != "comment"), None)
        module_pins = pin_index.module_pins(
            first_decl.start_point[0] if first_decl is not None else None
        )
        if module_pins:
            for decl in decls:
                if isinstance(decl, FunctionDecl):
                    decl.pins = sorted(set(decl.pins) | set(module_pins))
                elif isinstance(decl, ClassDecl):
                    for method in decl.methods:
                        method.pins = sorted(set(method.pins) | set(module_pins))
        return decls

    def _top_level(
        self,
        node: TSNode,
        source: bytes,
        rel_path: str,
        pin_index: PinIndex,
        outer: TSNode | None = None,
    ) -> list[Declaration]:
        outer = outer or node  # pins attach to the outermost node (`export ...`)
        t = node.type
        if t == "export_statement":
            inner = node.child_by_field_name("declaration")
            return (
                self._top_level(inner, source, rel_path, pin_index, outer=node)
                if inner is not None
                else []
            )
        if t == "function_declaration":
            return [
                self._function_decl(
                    node, source, is_method=False, pins=pin_index.for_definition(outer)
                )
            ]
        if t == "class_declaration":
            return [self._class_decl(node, source, pin_index)]
        if t in {"lexical_declaration", "variable_declaration"}:
            out: list[Declaration] = []
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                name_node = decl.child_by_field_name("name")
                value = decl.child_by_field_name("value")
                if name_node is None or name_node.type != "identifier":
                    continue
                name = _text(name_node, source)
                if value is not None and value.type in _FUNCTION_VALUES:
                    arrow = self._arrow_decl(node, value, name, source)
                    arrow.pins = pin_index.for_definition(outer)
                    out.append(arrow)
                else:
                    out.append(VariableDecl(node=node, name=name))
            return out
        if t == "import_statement":
            return list(self._imports(node, source, rel_path))
        return []

    def _class_decl(self, node: TSNode, source: bytes, pin_index: PinIndex) -> ClassDecl:
        name = (
            _text(node.child_by_field_name("name"), source)
            if node.child_by_field_name("name")
            else "<anonymous>"
        )
        methods: list[FunctionDecl] = []
        fields: dict[str, str] = {}
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                if child.type == "method_definition":
                    methods.append(
                        self._function_decl(
                            child,
                            source,
                            is_method=True,
                            pins=pin_index.for_definition(child),
                        )
                    )
                    if _node_name(child) == "constructor":
                        fields.update(_di_fields(child, source))
                elif child.type == "public_field_definition":
                    fname = child.child_by_field_name("name")
                    tname = _type_base(child.child_by_field_name("type"), source)
                    if fname is not None and tname:
                        fields[_text(fname, source)] = tname
        return ClassDecl(node=node, name=name, methods=methods, fields=fields)

    def _function_decl(
        self,
        node: TSNode,
        source: bytes,
        is_method: bool,
        pins: list[str] | None = None,
    ) -> FunctionDecl:
        name = _fn_name(node, source)
        return FunctionDecl(
            node=node,
            name=name,
            params=_params(node, source, is_method),
            signature=_signature(node, source, name),
            returns=_return_type(node, source),
            doc=_leading_doc(node, source),
            raises=_thrown_names(node, source),
            decorators=[],
            free_names=_free_names(node, source),
            pins=list(pins or []),
        )

    def _arrow_decl(
        self, decl_node: TSNode, arrow: TSNode, name: str, source: bytes
    ) -> FunctionDecl:
        return FunctionDecl(
            node=decl_node,
            name=name,
            params=_params(arrow, source, is_method=False),
            signature=_signature(arrow, source, name),
            returns=_return_type(arrow, source),
            doc=_leading_doc(decl_node, source),
            raises=_thrown_names(arrow, source),
            decorators=[],
            free_names=_free_names(arrow, source),
        )

    def _imports(self, node: TSNode, source: bytes, rel_path: str) -> Iterator[ImportDecl]:
        src_node = node.child_by_field_name("source")
        if src_node is None:
            return
        specifier = _string_value(src_node, source)
        base = _resolve_specifier(specifier, rel_path)
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            return
        for c in clause.named_children:
            if c.type == "identifier":  # default import
                yield ImportDecl(node=node, target=f"{base}.default", alias=_text(c, source))
            elif c.type == "named_imports":
                for spec in c.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name_node = spec.child_by_field_name("name")
                    alias_node = spec.child_by_field_name("alias")
                    if name_node is None:
                        continue
                    imported = _text(name_node, source)
                    local: str | None = (
                        _text(alias_node, source) if alias_node is not None else None
                    )
                    yield ImportDecl(node=node, target=f"{base}.{imported}", alias=local)
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    yield ImportDecl(node=node, target=base, alias=_text(ident, source))


# --- module-level helpers -----------------------------------------------------


def _text(node: TSNode | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_text(node: TSNode | None) -> str:
    """Node's own source text via tree-sitter (offset-safe, no file needed)."""
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text else ""


def _di_fields(ctor: TSNode, source: bytes) -> dict[str, str]:
    """Constructor params with an access modifier become class fields.

    ``constructor(private svc: ChaptersService)`` → ``{"svc": "ChaptersService"}``.
    Plain params (no modifier) are locals, not fields.
    """
    out: dict[str, str] = {}
    params = ctor.child_by_field_name("parameters")
    if params is None:
        return out
    for p in params.named_children:
        if p.type not in {"required_parameter", "optional_parameter"}:
            continue
        has_modifier = any(
            c.type in {"accessibility_modifier", "readonly", "override_modifier"}
            for c in p.children
        )
        if not has_modifier:
            continue
        pattern = p.child_by_field_name("pattern")
        tname = _type_base(p.child_by_field_name("type"), source)
        if pattern is not None and pattern.type == "identifier" and tname:
            out[_text(pattern, source)] = tname
    return out


def _type_base(type_annotation: TSNode | None, source: bytes) -> str | None:
    """Base class name of a ``: T`` annotation (``Chapter[]`` → ``Chapter``)."""
    if type_annotation is None:
        return None
    inner = next((c for c in type_annotation.named_children), None)
    if inner is None:
        return None
    if inner.type == "type_identifier":
        return _text(inner, source)
    if inner.type == "array_type":
        return _type_base_of_node(inner.named_children[0], source) if inner.named_children else None
    if inner.type == "generic_type":
        base = inner.child_by_field_name("name") or (
            inner.named_children[0] if inner.named_children else None
        )
        return _text(base, source).split("<")[0] if base is not None else None
    return None


def _type_base_of_node(node: TSNode, source: bytes) -> str | None:
    return _text(node, source) if node.type == "type_identifier" else None


def _node_name(node: TSNode) -> str:
    return _node_text(node.child_by_field_name("name"))


def _fn_name(node: TSNode, source: bytes) -> str:
    if node.type in {"function_declaration", "method_definition", "function_expression"}:
        return _text(node.child_by_field_name("name"), source)
    return ""


def _string_value(node: TSNode, source: bytes) -> str:
    for child in node.named_children:
        if child.type == "string_fragment":
            return _text(child, source)
    return _text(node, source).strip("\"'")


def _resolve_specifier(specifier: str, rel_path: str) -> str:
    """Map an import specifier to a module qualname (relative ones resolved).

    Resolved against the importing file's *path* (not its dotted module
    name) so Angular-style dotted filenames (``reader.component.ts``) don't
    get mis-split. ``"./chapters.service"`` from ``core/api/reader.component.ts``
    → ``core.api.chapters.service``.
    """
    if not specifier.startswith("."):
        return specifier  # bare package — stays opaque (third-party)
    base_dir = posixpath.dirname(rel_path.replace("\\", "/"))
    resolved = posixpath.normpath(posixpath.join(base_dir, specifier))
    for ext in (".ts", ".tsx", ".js"):
        resolved = resolved.removesuffix(ext)
    return resolved.strip("/").replace("/", ".")


def _classify_call(dotted: str) -> str | None:
    if any(ch in dotted for ch in "()[] \n"):
        return None
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[-1] in _DB_METHODS and parts[-2] in _DB_RECEIVERS:
        return "db"
    if len(parts) >= 2 and parts[-1] in _HTTP_VERBS and parts[-2] in _NET_RECEIVERS:
        return "net"
    if dotted.startswith(_IO_PREFIXES):
        return "io"
    if dotted in _NET_EXACT or dotted.startswith(_NET_PREFIXES):
        return "net"
    if dotted.startswith(_FS_PREFIXES) or dotted.endswith(_FS_METHOD_SUFFIXES):
        return "fs"
    if dotted in _NONDETERM_EXACT:
        return "nondeterm"
    return None


def _member_base(node: TSNode, source: bytes) -> list[str]:
    cur = node
    while cur.type in {"member_expression", "subscript_expression"}:
        nxt = cur.child_by_field_name("object")
        if nxt is None:
            return []
        cur = nxt
    return [_text(cur, source)] if cur.type == "identifier" else []


def _target_names(node: TSNode | None, source: bytes) -> list[str]:
    if node is None:
        return []
    if node.type == "identifier":
        return [_text(node, source)]
    if node.type in {"array_pattern", "object_pattern"}:
        out: list[str] = []
        for c in node.named_children:
            out.extend(_target_names(c, source))
        return out
    return []


def _lhs_split(left: TSNode, source: bytes) -> tuple[list[str], list[str]]:
    if left.type == "identifier":
        return [_text(left, source)], []
    if left.type in {"member_expression", "subscript_expression"}:
        return [], _member_base(left, source)
    if left.type in {"array_pattern", "object_pattern"}:
        return _target_names(left, source), []
    return [], []


def _reads_of(node: TSNode | None, source: bytes) -> list[str]:
    if node is None:
        return []
    if node.type == "parenthesized_expression":
        node = next((c for c in node.named_children), node)
    names: list[str] = []
    seen: set[str] = set()
    _collect_reads(node, source, names, seen)
    return names


def _collect_reads(node: TSNode, source: bytes, names: list[str], seen: set[str]) -> None:
    t = node.type
    if t == "identifier":
        name = _text(node, source)
        if name not in seen and name not in _JS_GLOBALS:
            seen.add(name)
            names.append(name)
        return
    if t == "member_expression":
        obj = node.child_by_field_name("object")
        if obj is not None:
            _collect_reads(obj, source, names, seen)
        return  # skip the property name
    if t == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            if obj is not None:
                _collect_reads(obj, source, names, seen)
        args = node.child_by_field_name("arguments")
        if args is not None:
            _collect_reads(args, source, names, seen)
        return
    for child in node.children:
        _collect_reads(child, source, names, seen)


def _call_mutations(stmt: TSNode, source: bytes) -> list[str]:
    names: list[str] = []
    stack: list[TSNode] = [stmt]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
                obj = fn.child_by_field_name("object")
                if prop is not None and obj is not None and _text(prop, source) in _MUTATOR_METHODS:
                    for base in _member_base(obj, source) or (
                        [_text(obj, source)] if obj.type == "identifier" else []
                    ):
                        if base not in names:
                            names.append(base)
        stack.extend(node.children)
    return names


def _arg_names(args_node: TSNode, source: bytes) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    _collect_reads(args_node, source, names, seen)
    return names


def _params(node: TSNode, source: bytes, is_method: bool) -> list[ParamDecl]:
    params_node = node.child_by_field_name("parameters")
    out: list[ParamDecl] = []
    if params_node is not None:
        for child in params_node.named_children:
            name = _param_name(child, source)
            if name and not (is_method and name == "this"):
                out.append(ParamDecl(name=name, node=child))
    else:
        single = node.child_by_field_name("parameter")  # arrow with one bare param
        if single is not None and single.type == "identifier":
            out.append(ParamDecl(name=_text(single, source), node=single))
    return out


def _param_name(node: TSNode, source: bytes) -> str | None:
    if node.type == "identifier":
        return _text(node, source)
    if node.type in {"required_parameter", "optional_parameter"}:
        pat = node.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            return _text(pat, source)
        for child in node.named_children:
            if child.type == "identifier":
                return _text(child, source)
    return None


def _return_type(node: TSNode, source: bytes) -> str | None:
    rt = node.child_by_field_name("return_type")
    if rt is None:
        return None
    inner = next((c for c in rt.named_children), None)
    return _text(inner, source) if inner is not None else None


def _signature(node: TSNode, source: bytes, name: str) -> str:
    params_node = node.child_by_field_name("parameters")
    params_text = _text(params_node, source) if params_node is not None else "()"
    ret = _return_type(node, source)
    sig = f"{name}{params_text}"
    if ret:
        sig += f": {ret}"
    return sig.replace("\n", " ")


def _leading_doc(node: TSNode, source: bytes) -> str:
    """A ``/** ... */`` JSDoc comment immediately preceding the declaration."""
    prev = node.prev_sibling
    if prev is not None and prev.type == "comment":
        raw = _text(prev, source).strip()
        if raw.startswith("/**"):
            body = raw.removeprefix("/**").removesuffix("*/")
            lines = [ln.strip().lstrip("*").strip() for ln in body.splitlines()]
            return " ".join(ln for ln in lines if ln).strip()
    return ""


def _thrown_names(node: TSNode, source: bytes) -> list[str]:
    body = node.child_by_field_name("body")
    if body is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    stack: list[TSNode] = [body]
    while stack:
        n = stack.pop()
        if n.type == "throw_statement":
            expr = next((c for c in n.named_children), None)
            if expr is not None and expr.type == "new_expression":
                ctor = expr.child_by_field_name("constructor")
                if ctor is not None:
                    name = _text(ctor, source).split(".")[-1]
                    if name and name not in seen:
                        seen.add(name)
                        names.append(name)
        stack.extend(n.children)
    return names


def _free_names(node: TSNode, source: bytes) -> list[str]:
    body = node.child_by_field_name("body")
    if body is None or body.type != "statement_block":
        return []
    bound: set[str] = set()
    for p in _params(node, source, is_method=False):
        bound.add(p.name)
    _collect_bound(body, source, bound)
    referenced: list[str] = []
    seen: set[str] = set()
    _collect_referenced(body, source, referenced, seen)
    return [n for n in referenced if n not in bound and n not in _JS_GLOBALS]


def _collect_bound(node: TSNode, source: bytes, bound: set[str]) -> None:
    t = node.type
    if t == "variable_declarator":
        name = node.child_by_field_name("name")
        if name is not None:
            bound.update(_target_names(name, source))
    elif t == "for_in_statement":
        left = node.child_by_field_name("left")
        if left is not None:
            bound.update(_target_names(left, source))
    elif t in {"function_declaration", "class_declaration"}:
        name = node.child_by_field_name("name")
        if name is not None:
            bound.add(_text(name, source))
    elif t == "catch_clause":
        param = node.child_by_field_name("parameter")
        if param is not None and param.type == "identifier":
            bound.add(_text(param, source))
    elif t == "required_parameter" or t == "optional_parameter":
        pn = _param_name(node, source)
        if pn:
            bound.add(pn)
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
    if t == "member_expression":
        obj = node.child_by_field_name("object")
        if obj is not None:
            _collect_referenced(obj, source, out, seen)
        return
    if t in {"import_statement"}:
        return
    for child in node.children:
        _collect_referenced(child, source, out, seen)
