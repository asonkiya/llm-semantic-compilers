"""GoAdapter — the Go implementation of :class:`LanguageAdapter`.

Mapping decisions (documented in ``docs/languages.md``):

* **package = directory.** Modules stay per-file (like the other adapters);
  same-package cross-file calls (idiomatic Go, no import needed) resolve via
  a directory-level symbol-table merge keyed on the module's language attr.
* **struct / interface types → Class nodes.** Struct members land in
  ``ClassDecl.fields`` — Go composition maps directly onto the DI machinery,
  so ``s.client.Fetch(id)`` resolves via the field's declared type.
* **``raise`` ≙ ``panic(``.** Go errors are values, not effects.
* **v1 known limits:** cross-*package* import resolution needs go.mod
  module-prefix stripping (follow-up); a struct declared in one file with
  methods in another weakens field-DI to same-file; ``select``/``defer``
  ordering is approximated (defer bodies still contribute effects — the
  whole body is walked); ``go f()`` is a plain call site.
"""

from __future__ import annotations

import tree_sitter_go
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
    ImportDecl,
    LanguageAdapter,
    LoopDesc,
    MatchDesc,
    ParamDecl,
    PinIndex,
    ReturnDesc,
    SimpleDesc,
    StatementDesc,
)

# --- effect tables (Go stdlib + common patterns) --------------------------------

_NET_PREFIXES: tuple[str, ...] = ("http.", "https.", "net.")
_IO_PREFIXES: tuple[str, ...] = ("fmt.Print", "fmt.Fprint", "log.", "println.")
_IO_EXACT: frozenset[str] = frozenset({"println", "print"})
_FS_PREFIXES: tuple[str, ...] = ("os.", "ioutil.", "filepath.Walk", "bufio.NewReader")
_FS_NOT: frozenset[str] = frozenset({"os.Getenv", "os.Exit", "os.Args"})
_NONDETERM_EXACT: frozenset[str] = frozenset(
    {
        "time.Now",
        "time.Since",
        "rand.Int",
        "rand.Intn",
        "rand.Float64",
        "uuid.New",
        "uuid.NewString",
    }
)
_NONDETERM_PREFIXES: tuple[str, ...] = ("rand.", "uuid.")
_DB_RECEIVERS: frozenset[str] = frozenset({"db", "tx", "txn", "conn", "stmt", "pool", "session"})
_DB_METHODS: frozenset[str] = frozenset(
    {
        "Query",
        "QueryRow",
        "QueryContext",
        "QueryRowContext",
        "Exec",
        "ExecContext",
        "Prepare",
        "Begin",
        "Commit",
        "Rollback",
        "Get",
        "Select",
    }
)

_FUNC_TYPES = frozenset({"function_declaration", "method_declaration"})


def _classify_call(dotted: str) -> str | None:
    if dotted in _FS_NOT:
        return None
    if dotted in _IO_EXACT or dotted.startswith(_IO_PREFIXES):
        return "io"
    if dotted.startswith(_NET_PREFIXES):
        return "net"
    if dotted.startswith(_FS_PREFIXES):
        return "fs"
    if dotted in _NONDETERM_EXACT or dotted.startswith(_NONDETERM_PREFIXES):
        return "nondeterm"
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[-2].lower() in _DB_RECEIVERS and parts[-1] in _DB_METHODS:
        return "db"
    return None


class GoAdapter(LanguageAdapter):
    name = "go"
    file_extensions = (".go",)

    def __init__(self) -> None:
        language = Language(tree_sitter_go.language())
        self._parser = Parser()
        self._parser.language = language

    def parse(self, source: bytes) -> TSNode:
        return self._parser.parse(source).root_node

    def locate_function(self, root: TSNode, name: str, start_row: int) -> TSNode | None:
        stack: list[TSNode] = [root]
        while stack:
            node = stack.pop()
            if node.type in _FUNC_TYPES and node.start_point[0] == start_row:
                if _fn_name(node, b"") is None:  # name via node text below
                    continue
                name_node = node.child_by_field_name("name")
                if name_node is not None and _node_text(name_node) == name:
                    return node
            stack.extend(node.children)
        return None

    # --- effects ---------------------------------------------------------------

    def direct_effects(self, func_node: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
        tags: set[str] = set()
        body = func_node.child_by_field_name("body")
        if body is None:
            return tags
        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    dotted = _node_text(fn)
                    if fn.type == "identifier" and dotted == "panic":
                        tags.add("raise")
                    else:
                        tag = _classify_call(dotted)
                        if tag is not None:
                            tags.add(tag)
            stack.extend(node.children)
        return tags

    # --- call graph --------------------------------------------------------------

    def call_sites(self, func_node: TSNode, source: bytes) -> list[CallSite]:
        sites: list[CallSite] = []
        body = func_node.child_by_field_name("body")
        if body is None:
            return sites
        # Go receivers are arbitrarily named (`func (s *Store) ...`); the
        # language-neutral field-call resolver keys on self/this, so
        # normalize `s.client.Fetch` -> `self.client.Fetch` here.
        receiver = None
        if func_node.type == "method_declaration":
            recv_list = func_node.child_by_field_name("receiver")
            if recv_list is not None:
                for pd in recv_list.named_children:
                    if pd.type == "parameter_declaration":
                        ident = next((c for c in pd.named_children if c.type == "identifier"), None)
                        if ident is not None:
                            receiver = _node_text(ident)
        stack: list[TSNode] = [body]
        while stack:
            node = stack.pop()
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None and fn.type in {"identifier", "selector_expression"}:
                    callee = _node_text(fn)
                    if receiver and callee.startswith(receiver + "."):
                        callee = "self." + callee[len(receiver) + 1 :]
                    if callee != "panic":
                        args = node.child_by_field_name("arguments")
                        sites.append((callee, _arg_names(args), node.start_point[0]))
            stack.extend(node.children)
        return sites

    # --- CFG ---------------------------------------------------------------------

    def function_body(self, func_node: TSNode) -> TSNode | None:
        return func_node.child_by_field_name("body")

    def block_statements(self, block: TSNode) -> list[TSNode]:
        return [c for c in block.named_children if c.type != "comment"]

    def describe_statement(self, node: TSNode, source: bytes) -> StatementDesc:
        t = node.type
        if t == "if_statement":
            alternative = node.child_by_field_name("alternative")
            else_block = (
                alternative if alternative is not None and alternative.type == "block" else None
            )
            next_branch = (
                alternative
                if alternative is not None and alternative.type == "if_statement"
                else None
            )
            return BranchDesc(
                reads=_reads_of(node.child_by_field_name("condition")),
                consequence=node.child_by_field_name("consequence"),
                else_block=else_block,
                next_branch=next_branch,
            )
        if t == "for_statement":
            return LoopDesc(
                reads=_loop_reads(node),
                writes=_loop_writes(node),
                body=node.child_by_field_name("body"),
            )
        if t == "return_statement":
            return ReturnDesc(reads=_reads_of(node))
        if t in {"short_var_declaration", "assignment_statement"}:
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            return AssignDesc(writes=_idents_of(left), reads=_reads_of(right))
        if t == "var_declaration":
            writes: list[str] = []
            reads: list[str] = []
            for spec in node.named_children:
                if spec.type == "var_spec":
                    name = spec.child_by_field_name("name")
                    if name is not None:
                        writes.extend(_idents_of(name))
                    value = spec.child_by_field_name("value")
                    reads.extend(_reads_of(value))
            return AssignDesc(writes=writes, reads=reads)
        if t in {"expression_switch_statement", "type_switch_statement", "select_statement"}:
            return self._describe_switch(node)
        return SimpleDesc(reads=_reads_of(node))

    def _describe_switch(self, node: TSNode) -> StatementDesc:
        subject_reads = _reads_of(node.child_by_field_name("value"))
        cases: list[CaseDesc] = []
        for child in node.named_children:
            if child.type in {"expression_case", "default_case", "type_case", "communication_case"}:
                # the case node itself carries its trailing statements; the CFG
                # builder walks block_statements(consequence) over it
                cases.append(CaseDesc(node=child, reads=list(subject_reads), consequence=child))
        if cases:
            return MatchDesc(cases=cases)
        return SimpleDesc(reads=subject_reads)

    # --- ingest --------------------------------------------------------------------

    def module_declarations(
        self, root: TSNode, source: bytes, module_name: str, rel_path: str
    ) -> list[Declaration]:
        pin_index = PinIndex(root, source)
        classes: dict[str, ClassDecl] = {}
        decls: list[Declaration] = []

        for child in root.named_children:
            t = child.type
            if t == "type_declaration":
                for spec in child.named_children:
                    if spec.type != "type_spec":
                        continue
                    name_node = spec.child_by_field_name("name")
                    type_node = spec.child_by_field_name("type")
                    if name_node is None or type_node is None:
                        continue
                    name = _node_text(name_node)
                    fields = (
                        _struct_fields(type_node)
                        if type_node.type in {"struct_type", "interface_type"}
                        else {}
                    )
                    classes[name] = ClassDecl(node=child, name=name, methods=[], fields=fields)
            elif t == "function_declaration":
                decls.append(self._function_decl(child, pins=pin_index.for_definition(child)))
            elif t == "import_declaration":
                decls.extend(_imports(child))

        # attach methods to their receiver's Class (create a stub if the type
        # is declared in a sibling file — same-package, documented limit).
        for child in root.named_children:
            if child.type != "method_declaration":
                continue
            receiver_type = _receiver_type(child)
            if receiver_type is None:
                continue
            if receiver_type not in classes:
                classes[receiver_type] = ClassDecl(node=child, name=receiver_type, methods=[])
            classes[receiver_type].methods.append(
                self._function_decl(child, pins=pin_index.for_definition(child))
            )

        decls.extend(classes.values())

        first_decl = next(
            (c for c in root.named_children if c.type not in {"comment", "package_clause"}), None
        )
        pinnable = {"function_declaration", "method_declaration", "type_declaration"}
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

    def _function_decl(self, node: TSNode, pins: list[str] | None = None) -> FunctionDecl:
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node is not None else "<anonymous>"
        params: list[ParamDecl] = []
        params_node = node.child_by_field_name("parameters")
        if params_node is not None:
            for pd in params_node.named_children:
                if pd.type != "parameter_declaration":
                    continue
                names = [c for c in pd.named_children if c.type == "identifier"]
                for ident in names:
                    params.append(ParamDecl(name=_node_text(ident), node=pd))
        result = node.child_by_field_name("result")
        return FunctionDecl(
            node=node,
            name=name,
            params=params,
            signature=_signature(node, name),
            returns=_node_text(result) if result is not None else None,
            doc="",
            raises=_panics(node),
            decorators=[],
            free_names=[],
            pins=list(pins or []),
        )


# --- helpers ----------------------------------------------------------------------


def _node_text(node: TSNode) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _fn_name(node: TSNode, _source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    return _node_text(name_node) if name_node is not None else None


def _receiver_type(method: TSNode) -> str | None:
    receiver = method.child_by_field_name("receiver")
    if receiver is None:
        return None
    for pd in receiver.named_children:
        if pd.type != "parameter_declaration":
            continue
        type_node = pd.child_by_field_name("type")
        if type_node is None:
            continue
        if type_node.type == "pointer_type" and type_node.named_child_count:
            type_node = type_node.named_children[0]
        if type_node.type == "type_identifier":
            return _node_text(type_node)
    return None


def _struct_fields(type_node: TSNode) -> dict[str, str]:
    """Struct/interface members: name -> type text (multi-name rows split)."""
    fields: dict[str, str] = {}
    body = next(
        (
            c
            for c in type_node.named_children
            if c.type in {"field_declaration_list", "method_spec_list"}
        ),
        None,
    )
    if body is None:
        return fields
    for member in body.named_children:
        if member.type == "field_declaration":
            names = [c for c in member.named_children if c.type == "field_identifier"]
            ftype = member.child_by_field_name("type")
            type_text = _base_type_text(ftype) if ftype is not None else ""
            for name in names:
                fields[_node_text(name)] = type_text
        elif member.type == "method_spec":
            spec_name = member.child_by_field_name("name")
            if spec_name is not None:
                fields[_node_text(spec_name)] = "func"
    return fields


def _base_type_text(type_node: TSNode) -> str:
    if type_node.type == "pointer_type" and type_node.named_child_count:
        return _node_text(type_node.named_children[0])
    return _node_text(type_node)


def _signature(node: TSNode, name: str) -> str:
    params = node.child_by_field_name("parameters")
    result = node.child_by_field_name("result")
    sig = f"{name}{_node_text(params) if params is not None else '()'}"
    if result is not None:
        sig += f" {_node_text(result)}"
    return sig


def _panics(node: TSNode) -> list[str]:
    body = node.child_by_field_name("body")
    if body is None:
        return []
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and _node_text(fn) == "panic":
                return ["panic"]
        stack.extend(n.children)
    return []


def _imports(node: TSNode) -> list[ImportDecl]:
    """Import paths as dotted targets; the bare package name is the alias.

    v1 limit: without go.mod module-prefix stripping these rarely resolve to
    in-repo modules — cross-package resolution is a documented follow-up.
    Same-package calls don't need imports at all (directory merge).
    """
    out: list[ImportDecl] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "import_spec":
            path_node = next(
                (c for c in n.named_children if c.type == "interpreted_string_literal"), None
            )
            if path_node is None:
                continue
            path = _node_text(path_node).strip('"')
            alias_node = n.child_by_field_name("name")
            alias = _node_text(alias_node) if alias_node is not None else path.rsplit("/", 1)[-1]
            out.append(ImportDecl(node=node, target=path.replace("/", "."), alias=alias))
        stack.extend(n.named_children)
    return out


def _idents_of(node: TSNode | None) -> list[str]:
    if node is None:
        return []
    names: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            text = _node_text(n)
            if text != "_":
                names.append(text)
        else:
            stack.extend(n.named_children)
    return names


def _reads_of(node: TSNode | None) -> list[str]:
    if node is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            text = _node_text(n)
            if text not in seen and text != "_":
                seen.add(text)
                names.append(text)
        elif n.type == "selector_expression":
            operand = n.child_by_field_name("operand")
            if operand is not None:
                stack.append(operand)
            continue
        else:
            stack.extend(n.named_children)
    return names


def _loop_reads(node: TSNode) -> list[str]:
    reads: list[str] = []
    for field_name in ("condition", "range"):  # plain and range loops
        reads.extend(_reads_of(node.child_by_field_name(field_name)))
    for child in node.named_children:
        if child.type == "range_clause":
            reads.extend(_reads_of(child.child_by_field_name("right")))
    return reads


def _loop_writes(node: TSNode) -> list[str]:
    writes: list[str] = []
    for child in node.named_children:
        if child.type == "range_clause":
            writes.extend(_idents_of(child.child_by_field_name("left")))
        elif child.type == "for_clause":
            init = child.child_by_field_name("initializer")
            if init is not None and init.type == "short_var_declaration":
                writes.extend(_idents_of(init.child_by_field_name("left")))
    return writes


def _arg_names(args_node: TSNode | None) -> list[str]:
    if args_node is None:
        return []
    names: list[str] = []
    for arg in args_node.named_children:
        if arg.type == "identifier":
            names.append(_node_text(arg))
    return names
