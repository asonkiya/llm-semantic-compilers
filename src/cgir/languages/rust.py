"""RustAdapter — the Rust implementation of :class:`LanguageAdapter`.

Originally written by an independent agent from ``docs/writing-an-adapter.md``
alone (the docs-usability experiment; see ``examples/rust-adapter/``), then
reviewed and promoted to a builtin. Maps tree-sitter-rust (0.23.x) to CGIR's
normalized descriptors: functions, structs + impl methods (struct fields power
DI resolution of ``self.field.method()``), use-declaration trees (incl.
grouped/aliased forms), CFG statement shapes (if/else-if, for/while/loop,
match arms, let/assign/compound-assign), effect tables with confidence tiers,
doc comments, attributes, and ``cgir:`` pins.

Known limits:
- enums, traits, and ``impl Trait for Type`` methods are not ingested as
  components (impl name extraction is best-effort for plain ``impl Type``);
- Rust's ``?`` operator is value-flow, not ``raise``; ``panic!``-family
  macros and ``.unwrap()``/``.expect()`` are tagged ``raise`` (aggressive but
  benign — raise is not impure in the taxonomy);
- implicit tail-expression returns classify as ``SimpleDesc``, not
  ``ReturnDesc``; ``#[cfg]`` conditional compilation is ignored;
- ``db``-receiver gating and bare ``.now()`` are lexical-confidence guesses.
"""

from __future__ import annotations

import tree_sitter_rust
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cgir.languages.base import (
    ADAPTER_API_VERSION,
    AssignDesc,
    BranchDesc,
    CallSite,
    CaseDesc,
    ClassDecl,
    Declaration,
    FunctionDecl,
    ImportDecl,
    LanguageAdapter,
    LoopDesc,
    MatchDesc,
    ParamDecl,
    PinIndex,
    ReturnDesc,
    SimpleDesc,
    StatementDesc,
    VariableDecl,
)

# ---------------------------------------------------------------------------
# Effect-detection tables
# ---------------------------------------------------------------------------

_IO_MACROS: frozenset[str] = frozenset({"println", "print", "eprintln", "eprint"})
_PANIC_MACROS: frozenset[str] = frozenset({"panic", "todo", "unimplemented", "unreachable"})

_FS_PREFIXES: tuple[str, ...] = (
    "std.fs.",
    "std.path.",
    "tokio.fs.",
    "async_std.fs.",
)
_FS_TYPE_PREFIXES: tuple[str, ...] = ("File::", "BufReader::", "BufWriter::")
_FS_DOTTED_PREFIXES: tuple[str, ...] = ("File.", "BufReader.", "BufWriter.")

_NET_PREFIXES: tuple[str, ...] = (
    "reqwest.",
    "reqwest::",
    "hyper.",
    "hyper::",
    "actix_web.",
    "tokio.net.",
    "std.net.",
    "surf.",
    "ureq.",
)
_NET_TYPE_PREFIXES: tuple[str, ...] = ("TcpStream::", "TcpListener::", "UdpSocket::")
_NET_DOTTED_PREFIXES: tuple[str, ...] = ("TcpStream.", "TcpListener.", "UdpSocket.")

_NONDETERM_PREFIXES: tuple[str, ...] = (
    "rand.",
    "rand::",
    "getrandom.",
)
_NONDETERM_EXACT: frozenset[str] = frozenset(
    {
        "std.time.SystemTime.now",
        "std.time.Instant.now",
        "SystemTime.now",
        "Instant.now",
    }
)

_DB_PREFIXES: tuple[str, ...] = (
    "sqlx.",
    "sqlx::",
    "diesel.",
    "diesel::",
    "sea_orm.",
    "rusqlite.",
)
_DB_RECEIVERS: frozenset[str] = frozenset(
    {"db", "pool", "conn", "connection", "tx", "txn", "session", "executor"}
)
_DB_METHODS: frozenset[str] = frozenset(
    {
        "query",
        "execute",
        "fetch",
        "fetch_all",
        "fetch_one",
        "fetch_optional",
        "insert",
        "update",
        "delete",
        "commit",
        "rollback",
        "begin",
        "prepare",
        "transaction",
    }
)

_PANIC_METHODS: frozenset[str] = frozenset({"unwrap", "expect"})

# Macros that are effects, not user-facing calls
_EFFECT_MACROS: frozenset[str] = (
    _IO_MACROS | _PANIC_MACROS | frozenset({"assert", "assert_eq", "assert_ne", "debug_assert"})
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text(node: TSNode, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_text(node: TSNode) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _scoped_id_to_dotted(node: TSNode) -> str:
    """Recursively convert a scoped_identifier to a dotted string.

    e.g. ``std::fs::File`` → ``std.fs.File``
    """
    text = _node_text(node)
    return text.replace("::", ".")


def _field_expr_dotted(node: TSNode) -> str:
    """Flatten a field_expression chain into a dotted string.

    ``self.client.get`` → ``self.client.get``
    Handles arbitrary nesting of field_expression nodes.
    """
    value = node.child_by_field_name("value")
    fld = node.child_by_field_name("field")
    fld_text = _node_text(fld) if fld is not None else ""

    if value is None:
        return fld_text

    if value.type == "self":
        prefix = "self"
    elif value.type == "identifier":
        prefix = _node_text(value)
    elif value.type == "field_expression":
        prefix = _field_expr_dotted(value)
    elif value.type == "scoped_identifier":
        prefix = _scoped_id_to_dotted(value)
    elif value.type == "call_expression":
        # chained call: c.get(...).send() — resolve the receiver of the chain
        fn = value.child_by_field_name("function")
        prefix = _callee_dotted(fn) if fn is not None else _node_text(value).split("(")[0]
    else:
        # fall back to raw text, strip call expressions
        raw = _node_text(value)
        prefix = raw.split("(")[0] if "(" in raw else raw

    return f"{prefix}.{fld_text}" if fld_text else prefix


def _callee_dotted(fn_node: TSNode) -> str:
    """Resolve any callee node to a dotted string."""
    t = fn_node.type
    if t == "identifier":
        return _node_text(fn_node)
    if t == "scoped_identifier":
        return _scoped_id_to_dotted(fn_node)
    if t == "field_expression":
        return _field_expr_dotted(fn_node)
    if t == "generic_function":
        # generic_function wraps identifier or scoped_identifier
        inner = fn_node.named_children[0] if fn_node.named_child_count else None
        if inner is not None:
            return _callee_dotted(inner)
    return _node_text(fn_node).replace("::", ".")


def _collect_reads_from(node: TSNode, source: bytes, into: list[str], seen: set[str]) -> None:
    """Walk node collecting data-bearing identifiers, not descending into nested blocks."""
    t = node.type
    if t in {"function_item", "closure_expression"}:
        return
    if t == "identifier":
        name = _node_text(node)
        if name not in seen:
            seen.add(name)
            into.append(name)
        return
    if t == "field_expression":
        # only track the root receiver
        val = node.child_by_field_name("value")
        if val is not None:
            _collect_reads_from(val, source, into, seen)
        return
    if t == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "field_expression":
            val = fn.child_by_field_name("value")
            if val is not None:
                _collect_reads_from(val, source, into, seen)
        elif fn is not None and fn.type == "identifier":
            pass  # callee is not a data read
        args = node.child_by_field_name("arguments")
        if args is not None:
            for child in args.named_children:
                _collect_reads_from(child, source, into, seen)
        return
    for child in node.named_children:
        _collect_reads_from(child, source, into, seen)


def _reads_of(node: TSNode | None, source: bytes) -> list[str]:
    if node is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    _collect_reads_from(node, source, names, seen)
    return names


def _idents_in_pattern(node: TSNode) -> list[str]:
    """Collect bound identifier names from a pattern node."""
    if node.type == "identifier":
        return [_node_text(node)]
    if node.type in {"tuple_pattern", "slice_pattern", "struct_pattern", "ref_pattern"}:
        names: list[str] = []
        for child in node.named_children:
            names.extend(_idents_in_pattern(child))
        return names
    # Just look for identifier descendants
    names = []
    for child in node.named_children:
        if child.type == "identifier":
            names.append(_node_text(child))
        elif child.type not in {":", "=", "mut"}:
            names.extend(_idents_in_pattern(child))
    return names


def _get_doc_comments(children: list[TSNode], fn_index: int) -> str:
    """Collect consecutive /// doc comments immediately before fn_index."""
    j = fn_index - 1
    # Walk backwards collecting doc comment lines
    collected: list[str] = []
    while j >= 0 and children[j].type == "line_comment":
        node = children[j]
        is_doc = any(c.type == "outer_doc_comment_marker" for c in node.children)
        if not is_doc:
            break  # stop at non-doc comment
        doc_node = next((c for c in node.named_children if c.type == "doc_comment"), None)
        if doc_node is not None:
            collected.append(_node_text(doc_node).strip())
        j -= 1
    collected.reverse()
    return "\n".join(collected)


def _get_attributes(children: list[TSNode], fn_index: int) -> list[str]:
    """Collect attribute_item nodes immediately before fn_index (skipping doc comments)."""
    attrs: list[str] = []
    j = fn_index - 1
    while j >= 0 and children[j].type in {"attribute_item", "line_comment"}:
        if children[j].type == "attribute_item":
            attr_node = children[j]
            # Get the attribute child's text
            attr = next((c for c in attr_node.named_children if c.type == "attribute"), None)
            if attr is not None:
                attrs.append(_node_text(attr).strip())
        j -= 1
    attrs.reverse()
    return attrs


def _type_name(type_node: TSNode | None) -> str | None:
    """Extract the base type name for field DI resolution."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "type_identifier":
        return _node_text(type_node)
    if t == "primitive_type":
        return _node_text(type_node)
    if t == "generic_type":
        # Vec<i32> -> Vec: DI matching wants the outer type name
        inner = type_node.named_children[0] if type_node.named_child_count else None
        if inner is not None and inner.type in {"type_identifier", "scoped_type_identifier"}:
            return _node_text(inner).split("::")[-1]
        return None
    if t == "reference_type":
        # &Foo, &mut Foo
        inner = type_node.child_by_field_name("type")
        return _type_name(inner)
    if t == "scoped_type_identifier":
        # e.g. std::fs::File -> File
        return _node_text(type_node).split("::")[-1]
    return None


def _extract_param_type(param_node: TSNode) -> str:
    """Get type annotation text from a parameter node."""
    type_node = param_node.child_by_field_name("type")
    if type_node is None:
        return ""
    return _node_text(type_node)


def _has_self_param(params_node: TSNode) -> bool:
    if params_node is None:
        return False
    return any(c.type == "self_parameter" for c in params_node.named_children)


def _build_signature(
    name: str,
    params_node: TSNode | None,
    return_type_node: TSNode | None,
) -> str:
    """Build a signature string like ``name(param: type, ...) -> ret``."""
    parts: list[str] = []
    if params_node is not None:
        for child in params_node.named_children:
            if child.type == "self_parameter":
                continue
            if child.type == "parameter":
                patt = child.child_by_field_name("pattern")
                ptype = child.child_by_field_name("type")
                pname = _node_text(patt) if patt is not None else "?"
                ptype_text = _node_text(ptype) if ptype is not None else ""
                if ptype_text:
                    parts.append(f"{pname}: {ptype_text}")
                else:
                    parts.append(pname)
    sig = f"{name}({', '.join(parts)})"
    if return_type_node is not None:
        ret_text = _node_text(return_type_node)
        if ret_text:
            sig += f" -> {ret_text}"
    return sig


def _extract_panics(func_node: TSNode) -> list[str]:
    """Return list of panic-like macro names found in function body."""
    body = func_node.child_by_field_name("body")
    if body is None:
        return []
    names: list[str] = []
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "macro_invocation":
            macro_name_node = node.named_children[0] if node.named_child_count else None
            if macro_name_node is not None and macro_name_node.type == "identifier":
                name = _node_text(macro_name_node)
                if name in _PANIC_MACROS and name not in names:
                    names.append(name)
        stack.extend(node.children)
    return names


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class RustAdapter(LanguageAdapter):
    """Rust language adapter for CGIR.

    Plugs into the TreeSitterSource pipeline as a language plugin.
    Register via entry point:
        [project.entry-points."cgir.languages"]
        rust = "rust_adapter:RustAdapter"
    """

    name = "rust"
    file_extensions = (".rs",)
    api_version = ADAPTER_API_VERSION

    def __init__(self) -> None:
        language = Language(tree_sitter_rust.language())
        self._parser = Parser()
        self._parser.language = language

    def parse(self, source: bytes) -> TSNode:
        return self._parser.parse(source).root_node

    def locate_function(self, root: TSNode, name: str, start_row: int) -> TSNode | None:
        """Find a function_item or impl method named ``name`` at ``start_row``."""
        stack: list[TSNode] = [root]
        while stack:
            node = stack.pop()
            if node.type == "function_item" and node.start_point[0] == start_row:
                name_node = node.child_by_field_name("name")
                if name_node is not None and _node_text(name_node) == name:
                    return node
            # Recurse into impl blocks
            if node.type == "impl_item":
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.extend(body.children)
                continue
            stack.extend(node.children)
        return None

    def function_index_entries(self, root: TSNode, source: bytes):
        stack: list[TSNode] = [root]
        while stack:
            node = stack.pop()
            if node.type == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    yield (_node_text(name_node), node.start_point[0], node)
            stack.extend(node.children)

    # --- effects ---------------------------------------------------------------

    def direct_effects(self, func_node: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
        return set(self.direct_effects_confidence(func_node, source, aliases))

    def direct_effects_confidence(
        self, func_node: TSNode, source: bytes, aliases: dict[str, str]
    ) -> dict[str, str]:
        tags: dict[str, str] = {}

        def add(tag: str, conf: str) -> None:
            if tags.get(tag) != "high":
                tags[tag] = conf

        body = func_node.child_by_field_name("body")
        if body is None:
            return tags

        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()

            if node.type == "macro_invocation":
                macro_id = next((c for c in node.named_children if c.type == "identifier"), None)
                if macro_id is not None:
                    mname = _node_text(macro_id)
                    if mname in _PANIC_MACROS:
                        add("raise", "high")
                    elif mname in _IO_MACROS:
                        add("io", "high")

            elif node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    dotted = _callee_dotted(fn)
                    # Normalize :: to .
                    dotted_norm = dotted.replace("::", ".")

                    # Alias resolution
                    head = dotted_norm.split(".")[0]
                    if head in aliases:
                        resolved = aliases[head]
                        tail = dotted_norm[len(head) :]
                        dotted_norm = resolved + tail

                    hit = _classify_rust_call_conf(dotted_norm)
                    if hit is not None:
                        add(*hit)

            stack.extend(node.children)

        return tags

    # --- call graph --------------------------------------------------------------

    def call_sites(self, func_node: TSNode, source: bytes) -> list[CallSite]:
        sites: list[CallSite] = []
        body = func_node.child_by_field_name("body")
        if body is None:
            return sites

        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()

            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    dotted = _callee_dotted(fn)
                    # Normalize :: to .
                    dotted = dotted.replace("::", ".")

                    # Skip effect-only calls
                    leaf = dotted.split(".")[-1]
                    if leaf not in _PANIC_METHODS:
                        args = node.child_by_field_name("arguments")
                        arg_names: list[str] = []
                        if args is not None:
                            for arg in args.named_children:
                                if arg.type == "identifier":
                                    arg_names.append(_node_text(arg))
                        sites.append((dotted, arg_names, node.start_point[0] + 1))

            elif node.type == "macro_invocation":
                # Emit macro calls that aren't effect-only macros
                macro_id = next((c for c in node.named_children if c.type == "identifier"), None)
                if macro_id is not None:
                    mname = _node_text(macro_id)
                    if mname not in _EFFECT_MACROS:
                        sites.append((mname, [], node.start_point[0] + 1))

            stack.extend(node.children)

        return sites

    # --- CFG ---------------------------------------------------------------------

    def function_body(self, func_node: TSNode) -> TSNode | None:
        return func_node.child_by_field_name("body")

    def block_statements(self, block: TSNode) -> list[TSNode]:
        return [
            c
            for c in block.named_children
            if c.type not in {"line_comment", "block_comment", "comment"}
        ]

    def describe_statement(self, node: TSNode, source: bytes) -> StatementDesc:
        t = node.type

        # Unwrap expression_statement wrapper
        if t == "expression_statement":
            inner = node.named_children[0] if node.named_child_count else None
            if inner is not None:
                return self.describe_statement(inner, source)
            return SimpleDesc()

        if t == "let_declaration":
            patt = node.child_by_field_name("pattern")
            val = node.child_by_field_name("value")
            writes: list[str] = _idents_in_pattern(patt) if patt is not None else []
            reads = _reads_of(val, source)
            return AssignDesc(writes=writes, reads=reads)

        if t == "compound_assignment_expr":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            writes = _idents_in_pattern(left) if left is not None else []
            reads = _reads_of(right, source)
            return AssignDesc(writes=writes, mutates=writes, reads=reads)

        if t == "if_expression":
            return self._describe_if(node, source)

        if t == "for_expression":
            patt = node.child_by_field_name("pattern")
            val = node.child_by_field_name("value")
            body = node.child_by_field_name("body")
            writes = _idents_in_pattern(patt) if patt is not None else []
            reads = _reads_of(val, source)
            return LoopDesc(reads=reads, writes=writes, body=body)

        if t == "while_expression":
            cond = node.child_by_field_name("condition")
            body = node.child_by_field_name("body")
            return LoopDesc(reads=_reads_of(cond, source), writes=[], body=body)

        if t == "loop_expression":
            body = node.child_by_field_name("body")
            return LoopDesc(reads=[], writes=[], body=body)

        if t == "match_expression":
            return self._describe_match(node, source)

        if t == "return_expression":
            # The returned expression is the first named child after "return" keyword
            ret_val = next((c for c in node.named_children if c.type != "return"), None)
            return ReturnDesc(reads=_reads_of(ret_val, source))

        if t == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            writes = []
            mutates: list[str] = []
            if left is not None:
                if left.type == "identifier":
                    writes = [_node_text(left)]
                elif left.type == "field_expression":
                    # self.field = ... → mutates self
                    val = left.child_by_field_name("value")
                    if val is not None:
                        if val.type == "self":
                            mutates = ["self"]
                        elif val.type == "identifier":
                            mutates = [_node_text(val)]
                elif left.type == "index_expression":
                    obj = left.child_by_field_name("value") or (
                        left.named_children[0] if left.named_child_count else None
                    )
                    if obj is not None and obj.type == "identifier":
                        mutates = [_node_text(obj)]
            return AssignDesc(writes=writes, mutates=mutates, reads=_reads_of(right, source))

        # Default
        return SimpleDesc(reads=_reads_of(node, source))

    def _describe_if(self, node: TSNode, source: bytes) -> BranchDesc:
        cond = node.child_by_field_name("condition")
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")  # else_clause

        else_block: TSNode | None = None
        next_branch: TSNode | None = None

        if alternative is not None and alternative.type == "else_clause":
            for child in alternative.named_children:
                if child.type == "block":
                    else_block = child
                    break
                elif child.type == "if_expression":
                    next_branch = child
                    break

        return BranchDesc(
            reads=_reads_of(cond, source),
            consequence=consequence,
            else_block=else_block,
            next_branch=next_branch,
        )

    def _describe_match(self, node: TSNode, source: bytes) -> StatementDesc:
        scrutinee = node.child_by_field_name("value")
        subject_reads = _reads_of(scrutinee, source)

        match_block = node.child_by_field_name("body")
        cases: list[CaseDesc] = []
        if match_block is not None:
            for arm in match_block.named_children:
                if arm.type != "match_arm":
                    continue
                arm_val = arm.child_by_field_name("value")
                # The consequence node is the arm's value (could be block or expr)
                cases.append(CaseDesc(node=arm, reads=list(subject_reads), consequence=arm_val))

        if cases:
            return MatchDesc(cases=cases)
        return SimpleDesc(reads=subject_reads)

    # --- ingest extraction -------------------------------------------------------

    def module_declarations(
        self, root: TSNode, source: bytes, module_name: str, rel_path: str
    ) -> list[Declaration]:
        pin_index = PinIndex(root, source)
        decls: list[Declaration] = []
        # Map from struct name to ClassDecl so we can attach impl methods
        classes: dict[str, ClassDecl] = {}

        # Build a list of top-level children for sibling-based doc/attr lookup
        children: list[TSNode] = list(root.children)

        # First pass: collect struct declarations
        for child in children:
            if child.type == "struct_item":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                struct_name = _node_text(name_node)
                fields = _extract_struct_fields(child)
                classes[struct_name] = ClassDecl(
                    node=child, name=struct_name, methods=[], fields=fields
                )

        # Second pass: functions, impl blocks, use declarations, const/static
        for i, child in enumerate(children):
            t = child.type
            if t == "function_item":
                decl = self._extract_function(child, children, i, source, pin_index)
                decls.append(decl)

            elif t == "impl_item":
                impl_name = _get_impl_type_name(child)
                if impl_name is None:
                    continue
                struct_name = impl_name
                if struct_name not in classes:
                    classes[struct_name] = ClassDecl(node=child, name=struct_name)

                impl_body = child.child_by_field_name("body")
                if impl_body is None:
                    continue
                impl_children = list(impl_body.children)
                for j, fn_node in enumerate(impl_children):
                    if fn_node.type == "function_item":
                        fn_decl = self._extract_function(
                            fn_node, impl_children, j, source, pin_index
                        )
                        classes[struct_name].methods.append(fn_decl)

            elif t == "use_declaration":
                decls.extend(_extract_imports(child, source))

            elif t == "const_item" or t == "static_item":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    decls.append(VariableDecl(node=child, name=_node_text(name_node)))

        decls.extend(classes.values())

        # Apply module-level pins
        first_decl = next(
            (
                c
                for c in children
                if c.type not in {"line_comment", "block_comment", "comment", "attribute_item"}
            ),
            None,
        )
        pinnable = {"function_item", "struct_item", "impl_item"}
        module_pins = pin_index.module_pins(
            first_decl.start_point[0]
            if first_decl is not None and first_decl.type in pinnable
            else None
        )
        if module_pins:
            for d in decls:
                if isinstance(d, FunctionDecl):
                    d.pins = sorted(set(d.pins) | set(module_pins))
                elif isinstance(d, ClassDecl):
                    for method in d.methods:
                        method.pins = sorted(set(method.pins) | set(module_pins))

        return decls

    def _extract_function(
        self,
        fn_node: TSNode,
        siblings: list[TSNode],
        index: int,
        source: bytes,
        pin_index: PinIndex,
    ) -> FunctionDecl:
        name_node = fn_node.child_by_field_name("name")
        name = _node_text(name_node) if name_node is not None else "<anonymous>"

        params_node = fn_node.child_by_field_name("parameters")
        return_type_node = fn_node.child_by_field_name("return_type")

        params: list[ParamDecl] = []
        if params_node is not None:
            for child in params_node.named_children:
                if child.type == "self_parameter":
                    continue  # skip self receiver
                if child.type == "parameter":
                    patt = child.child_by_field_name("pattern")
                    if patt is not None:
                        pname = _node_text(patt)
                        params.append(ParamDecl(name=pname, node=child))

        sig = _build_signature(name, params_node, return_type_node)
        returns = _node_text(return_type_node) if return_type_node is not None else None

        # Doc comments from preceding /// lines
        doc = _get_doc_comments(siblings, index)

        # Attribute decorators
        decorators = _get_attributes(siblings, index)

        # Raises from panic! calls
        raises = _extract_panics(fn_node)

        # Pins via pin index
        pins = pin_index.for_definition(fn_node)

        return FunctionDecl(
            node=fn_node,
            name=name,
            params=params,
            signature=sig,
            returns=returns,
            doc=doc,
            raises=raises,
            decorators=decorators,
            free_names=[],
            pins=pins,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _get_impl_type_name(impl_node: TSNode) -> str | None:
    """Get the struct name being impl'd.

    The field name is 'type' in the tree-sitter-rust grammar.
    """
    type_node = impl_node.child_by_field_name("type")
    if type_node is not None and type_node.type in {"type_identifier", "generic_type"}:
        if type_node.type == "generic_type":
            # e.g. impl<T> MyStruct<T> — get the base name
            inner = type_node.named_children[0] if type_node.named_child_count else None
            if inner is not None:
                return _node_text(inner)
        return _node_text(type_node)
    # Fallback: iterate named_children
    for child in impl_node.named_children:
        if child.type == "type_identifier":
            return _node_text(child)
    return None


def _extract_struct_fields(struct_node: TSNode) -> dict[str, str]:
    """Extract field_name → type_name from a struct_item."""
    fields: dict[str, str] = {}
    body = struct_node.child_by_field_name("body")
    if body is None:
        return fields
    for child in body.named_children:
        if child.type == "field_declaration":
            name_node = child.child_by_field_name("name")
            type_node = child.child_by_field_name("type")
            if name_node is None:
                continue
            field_name = _node_text(name_node)
            type_str = _type_name(type_node) or (_node_text(type_node) if type_node else "")
            fields[field_name] = type_str
    return fields


def _scoped_id_chain(node: TSNode) -> list[str]:
    """Return the list of identifier segments in a scoped_identifier."""
    if node.type == "identifier":
        return [_node_text(node)]
    if node.type == "scoped_identifier":
        # Recursively get left side, then add the right identifier
        parts: list[str] = []
        for child in node.children:
            if child.type == "scoped_identifier":
                parts.extend(_scoped_id_chain(child))
            elif child.type == "identifier":
                parts.append(_node_text(child))
        return parts
    return [_node_text(node)]


def _extract_imports(use_node: TSNode, source: bytes) -> list[ImportDecl]:
    """Yield ImportDecl(s) from a use_declaration node."""
    decls: list[ImportDecl] = []
    # Find the first named child which is the use path/tree
    use_child = next(
        (c for c in use_node.named_children if c.type != "visibility_modifier"),
        None,
    )
    if use_child is None:
        return decls

    _extract_use_tree(use_child, [], use_node, decls)
    return decls


def _extract_use_tree(
    node: TSNode,
    prefix_parts: list[str],
    use_node: TSNode,
    decls: list[ImportDecl],
) -> None:
    """Recursively expand a use tree into ImportDecl entries."""
    t = node.type

    if t == "identifier":
        # e.g. `use reqwest;`
        name = _node_text(node)
        parts = [*prefix_parts, name]
        target = ".".join(parts)
        decls.append(ImportDecl(node=use_node, target=target, alias=name))

    elif t == "scoped_identifier":
        # e.g. `use std::fs::File;` — full path
        parts = _scoped_id_chain(node)
        target = ".".join(parts)
        alias = parts[-1] if parts else target
        decls.append(ImportDecl(node=use_node, target=target, alias=alias))

    elif t == "scoped_use_list":
        # e.g. `use std::io::{Read, Write};`
        # The scoped prefix (std::io) is first child (scoped_identifier or identifier)
        prefix_node = next(
            (c for c in node.children if c.type in {"scoped_identifier", "identifier"}), None
        )
        use_list = next((c for c in node.named_children if c.type == "use_list"), None)

        new_prefix: list[str] = list(prefix_parts)
        if prefix_node is not None:
            new_prefix.extend(_scoped_id_chain(prefix_node))

        if use_list is not None:
            for item in use_list.named_children:
                _extract_use_tree(item, new_prefix, use_node, decls)

    elif t == "use_list":
        # `{Read, Write}` directly
        for item in node.named_children:
            _extract_use_tree(item, list(prefix_parts), use_node, decls)

    elif t == "use_as_clause":
        # `Postgres as PG`
        children_idents = [c for c in node.named_children if c.type == "identifier"]
        if len(children_idents) >= 2:
            original = children_idents[0]
            alias_id = children_idents[1]
            parts = [*prefix_parts, _node_text(original)]
            target = ".".join(parts)
            alias = _node_text(alias_id)
            decls.append(ImportDecl(node=use_node, target=target, alias=alias))
        elif len(children_idents) == 1:
            parts = [*prefix_parts, _node_text(children_idents[0])]
            target = ".".join(parts)
            decls.append(ImportDecl(node=use_node, target=target, alias=target.split(".")[-1]))

    elif t == "use_wildcard":
        # `use std::io::*;` — skip, no specific binding
        pass


def _classify_rust_call_conf(dotted: str) -> tuple[str, str] | None:
    """Classify a (::->. normalized) dotted callee to (effect_tag, confidence)."""
    # Skip computed expressions
    if any(ch in dotted for ch in "()[] \n"):
        dotted = dotted.split("(")[0].strip()

    leaf = dotted.split(".")[-1]

    # unwrap/expect → raise
    if leaf in _PANIC_METHODS:
        return ("raise", "high")

    # FS by prefix
    if dotted.startswith(_FS_PREFIXES):
        return ("fs", "high")
    # FS by type constructor
    if any(dotted.startswith(p) for p in _FS_DOTTED_PREFIXES) or any(
        dotted.startswith(p) for p in _FS_TYPE_PREFIXES
    ):
        return ("fs", "high")

    # Net by prefix
    if dotted.startswith(_NET_PREFIXES):
        return ("net", "high")
    if any(dotted.startswith(p) for p in _NET_DOTTED_PREFIXES) or any(
        dotted.startswith(p) for p in _NET_TYPE_PREFIXES
    ):
        return ("net", "high")

    # Nondeterm exact
    if dotted in _NONDETERM_EXACT:
        return ("nondeterm", "high")
    # Nondeterm by prefix
    if dotted.startswith(_NONDETERM_PREFIXES):
        return ("nondeterm", "high")
    # .now() suffix → lexical nondeterm
    if leaf == "now":
        return ("nondeterm", "lexical")

    # DB by prefix
    if dotted.startswith(_DB_PREFIXES):
        return ("db", "high")
    # DB by receiver name + method
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[-2].lower() in _DB_RECEIVERS and parts[-1] in _DB_METHODS:
        return ("db", "lexical")

    return None
