"""CAdapter — the C implementation of :class:`LanguageAdapter`.

Written by an independent agent from ``docs/writing-an-adapter.md`` alone
(second run of the docs-usability experiment; see ``examples/c-adapter/``),
then reviewed and promoted to a builtin.

Known limits / honesty notes:
- Function pointers returned from functions (e.g. `int (*make_fn())(int)`) may
  not have their name extracted correctly; deep pointer_declarator nesting is
  handled up to 2 levels but exotic declarators may be missed.
- Prototype declarations (function signatures without a body) are intentionally
  skipped — only function_definition nodes become FunctionDecls.
- typedef struct {...} Name is supported; typedef of a named struct
  (`typedef struct Foo Foo`) produces no ClassDecl (it's an alias, not a new
  type with fields).
- C has no methods; ClassDecl.methods is always []. Fields feed shape-drift.
- <system.h> includes are emitted as ImportDecls but will not resolve to
  in-repo modules (CGIR symbol resolution is fine with unresolvable imports —
  they are simply unresolved edges in the graph).
- global_declared_names returns empty set — C has no `global`/`nonlocal`
  keyword; you cannot distinguish global from local writes syntactically
  inside a function body without a full pre-pass.
- Effects rules marked "lexical" are pattern guesses (receiver named `db`,
  suffix-only matches). Rules marked "high" are exact/prefix matches against
  curated C stdlib/POSIX/SQLite3/MySQL/libpq tables.
- Dynamic dispatch through function pointers is not tracked.
- `abort()`, `exit()`, `_exit()`, and `assert()` failures are classified as
  `raise` (high) — there are no C exceptions, so this maps the closest C
  equivalent to the CGIR tag.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import tree_sitter_c
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
# Effect tables
# ---------------------------------------------------------------------------

# "high" confidence: exact function name or prefix match
_IO_HIGH: frozenset[str] = frozenset(
    {
        "printf",
        "fprintf",
        "sprintf",
        "snprintf",
        "vprintf",
        "vfprintf",
        "vsprintf",
        "vsnprintf",
        "puts",
        "putchar",
        "putc",
        "fputc",
        "fputs",
        "scanf",
        "fscanf",
        "sscanf",
        "vscanf",
        "vfscanf",
        "vsscanf",
        "getchar",
        "getc",
        "fgetc",
        "fgets",
        "gets",
        "perror",
        "print",  # common wrapper alias
    }
)

_FS_HIGH: frozenset[str] = frozenset(
    {
        "fopen",
        "fclose",
        "fread",
        "fwrite",
        "fseek",
        "ftell",
        "rewind",
        "fflush",
        "feof",
        "ferror",
        "clearerr",
        "fileno",
        "tmpfile",
        "tmpnam",
        "open",
        "close",
        "read",
        "write",
        "pread",
        "pwrite",
        "lseek",
        "unlink",
        "remove",
        "rename",
        "mkdir",
        "rmdir",
        "stat",
        "lstat",
        "fstat",
        "access",
        "truncate",
        "ftruncate",
        "link",
        "symlink",
        "readlink",
        "opendir",
        "readdir",
        "closedir",
        "fdopen",
        "freopen",
    }
)

_NET_HIGH: frozenset[str] = frozenset(
    {
        "socket",
        "connect",
        "bind",
        "listen",
        "accept",
        "send",
        "recv",
        "sendto",
        "recvfrom",
        "sendmsg",
        "recvmsg",
        "shutdown",
        "gethostbyname",
        "getaddrinfo",
        "freeaddrinfo",
        "getnameinfo",
        "setsockopt",
        "getsockopt",
        "curl_easy_init",
        "curl_easy_setopt",
        "curl_easy_perform",
        "curl_easy_cleanup",
    }
)

_NONDETERM_HIGH: frozenset[str] = frozenset(
    {
        "rand",
        "srand",
        "random",
        "srandom",
        "rand_r",
        "time",
        "gettimeofday",
        "clock_gettime",
        "clock",
        "getpid",
        "getppid",
        "getenv",
    }
)

# prefix-based high matches (SQLite3, MySQL, libpq)
_DB_PREFIXES: tuple[str, ...] = ("sqlite3_", "mysql_", "PQ", "pg_")

_RAISE_HIGH: frozenset[str] = frozenset(
    {
        "abort",
        "exit",
        "_exit",
        "__exit",
        "quick_exit",
        "assert",  # macro, but shows up as call_expression in the grammar
        "err",
        "errx",
        "verr",
        "verrx",  # BSD err.h family
    }
)

# "lexical" confidence: receiver-name gating
_DB_RECEIVER_NAMES: frozenset[str] = frozenset({"db", "conn", "pg", "mysql", "sqlite"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text(node: TSNode) -> str:
    """Decode node source bytes safely."""
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _find_function_declarator(node: TSNode) -> TSNode | None:
    """Recursively find function_declarator inside pointer/abstract wrapping."""
    if node.type == "function_declarator":
        return node
    for child in node.children:
        result = _find_function_declarator(child)
        if result is not None:
            return result
    return None


def _extract_function_name(func_def: TSNode) -> str | None:
    """Extract the plain identifier name from a function_definition node."""
    # Direct child could be function_declarator or pointer_declarator wrapping it
    for child in func_def.children:
        fd = _find_function_declarator(child)
        if fd is not None:
            name_node = fd.child_by_field_name("declarator")
            if name_node is not None and name_node.type == "identifier":
                return _text(name_node)
            # Sometimes the name is a direct named child
            for c in fd.named_children:
                if c.type == "identifier":
                    return _text(c)
    return None


def _extract_params(func_def: TSNode) -> list[ParamDecl]:
    """Extract parameter declarations from a function_definition node."""
    params: list[ParamDecl] = []
    for child in func_def.children:
        fd = _find_function_declarator(child)
        if fd is None:
            continue
        param_list = fd.child_by_field_name("parameters")
        if param_list is None:
            continue
        for param in param_list.named_children:
            if param.type != "parameter_declaration":
                continue
            # Find the identifier in the param (may be inside pointer_declarator)
            param_name = _param_identifier(param)
            if param_name and param_name != "void":
                params.append(ParamDecl(name=param_name, node=param))
    return params


def _param_identifier(param: TSNode) -> str | None:
    """Find the plain identifier name in a parameter_declaration."""
    # Check direct identifier child
    for child in param.named_children:
        if child.type == "identifier":
            return _text(child)
        # pointer_declarator, array_declarator etc. wrap the name
        if child.type in ("pointer_declarator", "array_declarator", "reference_declarator"):
            inner = _declarator_identifier(child)
            if inner:
                return inner
    return None


def _declarator_identifier(node: TSNode) -> str | None:
    """Recursively extract identifier from a declarator node."""
    if node.type == "identifier":
        return _text(node)
    for child in node.named_children:
        result = _declarator_identifier(child)
        if result:
            return result
    return None


def _signature(func_def: TSNode, name: str, params: list[ParamDecl]) -> str:
    """Build a human-readable signature string."""
    param_strs = [p.name for p in params]
    return f"{name}({', '.join(param_strs)})"


def _collect_identifiers(node: TSNode, out: list[str]) -> None:
    """Recursively collect all identifier nodes under node."""
    if node.type == "identifier":
        out.append(_text(node))
    for child in node.named_children:
        _collect_identifiers(child, out)


def _condition_ids(node: TSNode) -> list[str]:
    """Extract identifiers from a parenthesized_expression (condition)."""
    ids: list[str] = []
    _collect_identifiers(node, ids)
    return list(dict.fromkeys(ids))  # deduplicated, order-preserving


def _walk_calls(node: TSNode) -> Iterator[TSNode]:
    """Walk all call_expression nodes in the subtree."""
    if node.type == "call_expression":
        yield node
    for child in node.children:
        yield from _walk_calls(child)


def _dotted_callee(func_expr: TSNode) -> str:
    """Convert a call function node into dotted text (for field expressions)."""
    if func_expr.type == "identifier":
        return _text(func_expr)
    if func_expr.type == "field_expression":
        obj = func_expr.child_by_field_name("argument")
        field = func_expr.child_by_field_name("field")
        if obj is not None and field is not None:
            return f"{_dotted_callee(obj)}.{_text(field)}"
    return _text(func_expr)


def _call_arg_ids(call: TSNode) -> list[str]:
    """Extract identifier arguments from a call_expression."""
    arg_list = call.child_by_field_name("arguments")
    if arg_list is None:
        return []
    ids = []
    for arg in arg_list.named_children:
        if arg.type == "identifier":
            ids.append(_text(arg))
    return ids


def _lhs_writes_mutates(lhs: TSNode) -> tuple[list[str], list[str]]:
    """
    Given the LHS of an assignment, return (writes, mutates).
    writes: plain variable names bound
    mutates: base names when LHS is field/pointer/array access
    """
    if lhs.type == "identifier":
        return ([_text(lhs)], [])
    if lhs.type == "field_expression":
        # p->x or p.x  → mutates "p"
        obj = lhs.child_by_field_name("argument")
        if obj is not None:
            return ([], [_declarator_identifier(obj) or _text(obj)])
        return ([], [])
    if lhs.type in ("subscript_expression", "pointer_expression"):
        # xs[i] or *p → mutates base
        base = lhs.named_children[0] if lhs.named_children else None
        if base is not None and base.type == "identifier":
            return ([], [_text(base)])
        return ([], [])
    # compound pointer/array derefs
    return ([], [])


def _rhs_reads(node: TSNode) -> list[str]:
    """Collect identifiers read from a RHS expression node."""
    ids: list[str] = []
    _collect_identifiers(node, ids)
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class CAdapter(LanguageAdapter):
    """CGIR language adapter for C (C99/C11)."""

    name = "c"
    file_extensions = (".c", ".h")
    api_version = ADAPTER_API_VERSION

    def __init__(self) -> None:
        lang = Language(tree_sitter_c.language())
        self._parser = Parser(lang)

    # ------------------------------------------------------------------
    # parse / locate
    # ------------------------------------------------------------------

    def parse(self, source: bytes) -> TSNode:
        return self._parser.parse(source).root_node

    def function_index_entries(
        self, root: TSNode, source: bytes
    ) -> Iterator[tuple[str, int, TSNode]]:
        stack: list[TSNode] = [root]
        while stack:
            node = stack.pop()
            if node.type == "function_definition":
                n = _extract_function_name(node)
                if n:
                    yield (n, node.start_point[0], node)
            stack.extend(node.children)

    def locate_function(self, root: TSNode, name: str, start_row: int) -> TSNode | None:
        """Find a function_definition by name and start row (0-based)."""
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "function_definition":
                n = _extract_function_name(node)
                if n == name and node.start_point[0] == start_row:
                    return node
            stack.extend(node.children)
        return None

    # ------------------------------------------------------------------
    # module_declarations
    # ------------------------------------------------------------------

    def module_declarations(
        self,
        root: TSNode,
        source: bytes,
        module_name: str,
        rel_path: str,
    ) -> list[Declaration]:
        pin_index = PinIndex(root, source)
        decls: list[Declaration] = []

        # Determine the first non-comment node for module_pins heuristic
        pinnable_types = {"function_definition", "struct_specifier", "type_definition"}
        first = next(
            (c for c in root.named_children if c.type not in ("comment",)),
            None,
        )
        first_row: int | None = (
            first.start_point[0] if first is not None and first.type in pinnable_types else None
        )

        for child in root.named_children:
            t = child.type

            # --- imports ---
            if t == "preproc_include":
                import_decl = self._process_include(child, source)
                if import_decl is not None:
                    decls.append(import_decl)

            # --- free functions ---
            elif t == "function_definition":
                fd = self._process_function(child, source, pin_index)
                if fd is not None:
                    decls.append(fd)

            # --- named struct: struct Foo { ... }; ---
            elif t == "struct_specifier":
                cd = self._process_struct(child, source, pin_index)
                if cd is not None:
                    decls.append(cd)

            # --- typedef struct { ... } Name; or typedef struct Foo { ... } Alias; ---
            elif t == "type_definition":
                items = self._process_typedef(child, source, pin_index)
                decls.extend(items)

            # --- top-level variable / constant ---
            elif t == "declaration":
                vd = self._process_variable(child, source)
                if vd is not None:
                    decls.extend(vd)

        # Apply module-level pins
        module_pins = pin_index.module_pins(first_row)
        if module_pins:
            for decl in decls:
                if isinstance(decl, FunctionDecl):
                    decl.pins = sorted(set(decl.pins) | set(module_pins))
                elif isinstance(decl, ClassDecl):
                    for m in decl.methods:
                        m.pins = sorted(set(m.pins) | set(module_pins))

        return decls

    def _process_include(self, node: TSNode, source: bytes) -> ImportDecl | None:
        """Convert a preproc_include node to ImportDecl."""
        for child in node.named_children:
            if child.type == "string_literal":
                # quoted include → local module
                # Get the content inside quotes
                content = child.child_by_field_name("value")
                if content is None:
                    # try extracting string_content child
                    for sc in child.named_children:
                        if sc.type == "string_content":
                            raw = _text(sc)
                            # "local.h" → "local" (strip .h, convert / to .)
                            dotted = re.sub(r"\.h$", "", raw).replace("/", ".").replace("-", "_")
                            return ImportDecl(
                                node=node,
                                target=dotted,
                                alias=dotted.split(".")[-1],
                            )
                break
            elif child.type == "system_lib_string":
                # <stdio.h> → skip (system header, won't resolve to in-repo module)
                # Emit it anyway as an unresolvable ImportDecl so the graph has the edge.
                raw = _text(child).strip("<>")
                dotted = re.sub(r"\.h$", "", raw).replace("/", ".").replace("-", "_")
                return ImportDecl(node=node, target=dotted, alias=dotted.split(".")[-1])
        return None

    def _process_function(
        self, node: TSNode, source: bytes, pin_index: PinIndex
    ) -> FunctionDecl | None:
        """Convert a function_definition node to FunctionDecl."""
        name = _extract_function_name(node)
        if name is None:
            return None
        params = _extract_params(node)
        sig = _signature(node, name, params)
        doc = self._leading_comment(node, source)
        pins = pin_index.for_definition(node)
        return FunctionDecl(
            node=node,
            name=name,
            params=params,
            signature=sig,
            returns=None,  # return type is available but not required by ABC
            doc=doc,
            raises=[],
            decorators=[],
            free_names=[],
            pins=pins,
        )

    def _leading_comment(self, node: TSNode, source: bytes) -> str:
        """Extract the immediately preceding block/line comment as docstring."""
        start_row = node.start_point[0]
        # Walk backwards through siblings at the same level
        # We don't have a parent reference, so scan source lines above
        lines = source.decode("utf-8", errors="replace").splitlines()
        doc_lines: list[str] = []
        row = start_row - 1
        while row >= 0:
            line = lines[row].strip() if row < len(lines) else ""
            if line.startswith("//"):
                doc_lines.insert(0, line.lstrip("/ ").strip())
                row -= 1
            elif line.endswith("*/"):
                # block comment
                while row >= 0:
                    bline = lines[row]
                    doc_lines.insert(0, bline.strip().lstrip("/*").strip())
                    if "/*" in bline:
                        break
                    row -= 1
                break
            else:
                break
        return "\n".join(doc_lines)

    def _process_struct(self, node: TSNode, source: bytes, pin_index: PinIndex) -> ClassDecl | None:
        """Convert a named struct_specifier to ClassDecl."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _text(name_node)
        fields = self._extract_fields(node)
        if not fields and node.child_by_field_name("body") is None:
            # Forward declaration only — no fields
            return None
        return ClassDecl(node=node, name=name, methods=[], fields=fields)

    def _process_typedef(
        self, node: TSNode, source: bytes, pin_index: PinIndex
    ) -> list[Declaration]:
        """Handle typedef — typedef struct { ... } Name; or typedef struct Foo {...} Alias."""
        decls: list[Declaration] = []
        # Find struct_specifier child and the trailing type_identifier (name)
        struct_node: TSNode | None = None
        type_name: str | None = None

        for child in node.named_children:
            if child.type == "struct_specifier":
                struct_node = child
            elif child.type == "type_identifier":
                type_name = _text(child)

        if struct_node is None or type_name is None:
            return decls

        fields = self._extract_fields(struct_node)
        body = struct_node.child_by_field_name("body")
        if body is None:
            # typedef struct Foo Bar  — just an alias, no new fields
            return decls

        decls.append(ClassDecl(node=node, name=type_name, methods=[], fields=fields))
        return decls

    def _extract_fields(self, struct_node: TSNode) -> dict[str, str]:
        """Extract field name → type text from a struct_specifier."""
        fields: dict[str, str] = {}
        body = struct_node.child_by_field_name("body")
        if body is None:
            return fields
        for child in body.named_children:
            if child.type != "field_declaration":
                continue
            # Get type text (first named child that's a type node)
            type_text = ""
            for tc in child.named_children:
                if tc.type in (
                    "primitive_type",
                    "type_identifier",
                    "struct_specifier",
                    "type_qualifier",
                ):
                    type_text = _text(tc)
                    break
            # Get field name(s): field_identifier or pointer/array wrapped
            for fc in child.named_children:
                fname = self._field_declarator_name(fc)
                if fname:
                    fields[fname] = type_text
        return fields

    def _field_declarator_name(self, node: TSNode) -> str | None:
        """Extract the field identifier from a field_declaration child."""
        if node.type == "field_identifier":
            return _text(node)
        if node.type in ("pointer_declarator", "array_declarator"):
            for child in node.named_children:
                result = self._field_declarator_name(child)
                if result:
                    return result
        return None

    def _process_variable(self, node: TSNode, source: bytes) -> list[VariableDecl]:
        """Convert a top-level declaration to VariableDecl(s)."""
        decls = []
        for child in node.named_children:
            if child.type == "init_declarator":
                id_node = child.child_by_field_name("declarator")
                if id_node is not None:
                    name = _declarator_identifier(id_node)
                    if name:
                        decls.append(VariableDecl(node=node, name=name))
            elif child.type == "identifier":
                decls.append(VariableDecl(node=node, name=_text(child)))
        return decls

    # ------------------------------------------------------------------
    # call_sites
    # ------------------------------------------------------------------

    def call_sites(self, func_node: TSNode, source: bytes) -> list[CallSite]:
        """Return all call sites in a function body."""
        body = self.function_body(func_node)
        if body is None:
            return []
        sites: list[CallSite] = []
        for call in _walk_calls(body):
            func_expr = call.child_by_field_name("function")
            if func_expr is None:
                continue
            callee = _dotted_callee(func_expr)
            args = _call_arg_ids(call)
            line = call.start_point[0]
            sites.append((callee, args, line))
        return sites

    # ------------------------------------------------------------------
    # effects
    # ------------------------------------------------------------------

    def direct_effects(self, func_node: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
        return set(self.direct_effects_confidence(func_node, source, aliases))

    def direct_effects_confidence(
        self, func_node: TSNode, source: bytes, aliases: dict[str, str]
    ) -> dict[str, str]:
        body = self.function_body(func_node)
        return self.classify_calls(body, source, aliases) if body is not None else {}

    def classify_calls(
        self, node: TSNode, source: bytes, aliases: dict[str, str]
    ) -> dict[str, str]:
        tags: dict[str, str] = {}
        for call in _walk_calls(node):
            func_expr = call.child_by_field_name("function")
            if func_expr is None:
                continue
            callee = _dotted_callee(func_expr)
            # Alias-normalize the callee
            parts = callee.split(".", 1)
            if parts[0] in aliases:
                callee = aliases[parts[0]] + ("." + parts[1] if len(parts) > 1 else "")

            # High-confidence exact matches
            base = callee.split(".")[-1]  # last segment for field calls
            plain = callee.split(".")[0]  # first segment (receiver or bare name)

            if base in _RAISE_HIGH or plain in _RAISE_HIGH:
                tags["raise"] = "lexical"  # raise-diff is never load-bearing; see python.py

            if base in _IO_HIGH or plain in _IO_HIGH:
                tags["io"] = "high"

            if base in _FS_HIGH or plain in _FS_HIGH:
                tags["fs"] = "high"

            if base in _NET_HIGH or plain in _NET_HIGH:
                tags["net"] = "high"

            if base in _NONDETERM_HIGH or plain in _NONDETERM_HIGH:
                tags["nondeterm"] = "high"

            # DB prefix match (high)
            for prefix in _DB_PREFIXES:
                if base.startswith(prefix) or plain.startswith(prefix):
                    tags["db"] = "high"
                    break

            # Lexical: receiver named like a db handle
            if "." in callee:
                receiver = callee.split(".")[0]
                if receiver in _DB_RECEIVER_NAMES and "db" not in tags:
                    # don't downgrade an existing high
                    tags["db"] = "lexical"

        return tags

    # ------------------------------------------------------------------
    # CFG support
    # ------------------------------------------------------------------

    def function_body(self, func_node: TSNode) -> TSNode | None:
        """Return the compound_statement body of a function_definition."""
        return func_node.child_by_field_name("body")

    def block_statements(self, block: TSNode) -> list[TSNode]:
        """Return statement nodes from a block, filtering comment nodes."""
        return [c for c in block.named_children if c.type not in ("comment",)]

    def describe_statement(self, node: TSNode, source: bytes) -> StatementDesc:
        """Classify one statement node into a StatementDesc."""
        t = node.type

        # --- if statement ---
        if t == "if_statement":
            return self._describe_if(node, source)

        # --- loops ---
        if t in ("for_statement", "while_statement", "do_statement"):
            return self._describe_loop(node, source)

        # --- switch ---
        if t == "switch_statement":
            return self._describe_switch(node, source)

        # --- return ---
        if t == "return_statement":
            reads: list[str] = []
            for child in node.named_children:
                if child.type not in ("comment",):
                    _collect_identifiers(child, reads)
            return ReturnDesc(reads=list(dict.fromkeys(reads)), mutates=[])

        # --- declaration with initializer (let-binding) ---
        if t == "declaration":
            return self._describe_declaration(node, source)

        # --- expression_statement wrapping an assignment or call ---
        if t == "expression_statement":
            return self._describe_expression_stmt(node, source)

        # --- break/continue/goto --- treat as SimpleDesc
        if t in ("break_statement", "continue_statement", "goto_statement"):
            return SimpleDesc(reads=[], mutates=[])

        # --- compound_statement (nested block without an owner) ---
        if t == "compound_statement":
            return SimpleDesc(reads=[], mutates=[])

        # --- fallback ---
        reads2: list[str] = []
        _collect_identifiers(node, reads2)
        return SimpleDesc(reads=list(dict.fromkeys(reads2)), mutates=[])

    def _describe_if(self, node: TSNode, source: bytes) -> BranchDesc:
        cond = node.child_by_field_name("condition")
        reads = _condition_ids(cond) if cond is not None else []
        consequence = node.child_by_field_name("consequence")
        else_clause = node.child_by_field_name("alternative")

        else_block: TSNode | None = None
        next_branch: TSNode | None = None

        if else_clause is not None:
            # else_clause wraps either a compound_statement or another if_statement
            for child in else_clause.named_children:
                if child.type == "if_statement":
                    next_branch = child
                elif child.type == "compound_statement":
                    else_block = child
                elif child.type not in ("comment",):
                    # bare else body (uncommon in well-formed C)
                    else_block = child

        return BranchDesc(
            reads=reads,
            consequence=consequence,
            else_block=else_block,
            next_branch=next_branch,
        )

    def _describe_loop(self, node: TSNode, source: bytes) -> LoopDesc:
        t = node.type
        reads: list[str] = []
        writes: list[str] = []

        if t == "for_statement":
            # for (init; cond; update) body
            # Collect reads from condition (second child after '(')
            # tree-sitter-c does NOT use field names for for-statement parts
            # Walk children: declaration/expression, expression, expression, body
            children = [c for c in node.named_children]
            # First named child is init (declaration or expression)
            # We scan all for identifiers (simplistic but correct for CFG)
            body = node.child_by_field_name("body")
            for child in children:
                if child == body:
                    break
                _collect_identifiers(child, reads)
            # Check if init is a declaration → extract write
            for child in node.named_children:
                if child.type == "declaration":
                    for dc in child.named_children:
                        if dc.type == "init_declarator":
                            id_node = dc.child_by_field_name("declarator")
                            if id_node is not None:
                                name = _declarator_identifier(id_node)
                                if name:
                                    writes.append(name)
                        elif dc.type == "identifier":
                            writes.append(_text(dc))
                    break
            reads = list(dict.fromkeys(reads))
        elif t in {"while_statement", "do_statement"}:
            cond = node.child_by_field_name("condition")
            if cond is not None:
                _collect_identifiers(cond, reads)
            reads = list(dict.fromkeys(reads))

        body = node.child_by_field_name("body")
        return LoopDesc(reads=reads, writes=writes, body=body)

    def _describe_switch(self, node: TSNode, source: bytes) -> MatchDesc:
        cond = node.child_by_field_name("condition")
        scrutinee = _condition_ids(cond) if cond is not None else []
        body = node.child_by_field_name("body")
        cases: list[CaseDesc] = []
        if body is not None:
            for child in body.named_children:
                if child.type == "case_statement":
                    cases.append(CaseDesc(node=child, reads=scrutinee, consequence=child))
        return MatchDesc(cases=cases)

    def _describe_declaration(self, node: TSNode, source: bytes) -> AssignDesc:
        writes: list[str] = []
        mutates: list[str] = []
        reads: list[str] = []
        for child in node.named_children:
            if child.type == "init_declarator":
                id_node = child.child_by_field_name("declarator")
                val_node = child.child_by_field_name("value")
                if id_node is not None:
                    name = _declarator_identifier(id_node)
                    if name:
                        writes.append(name)
                if val_node is not None:
                    _collect_identifiers(val_node, reads)
            elif child.type == "identifier":
                writes.append(_text(child))
            # Skip type nodes
        return AssignDesc(
            writes=writes,
            mutates=mutates,
            reads=list(dict.fromkeys(reads)),
        )

    def _describe_expression_stmt(self, node: TSNode, source: bytes) -> StatementDesc:
        # Unwrap the expression_statement's single expression child
        expr = next(
            (c for c in node.named_children if c.type not in ("comment",)),
            None,
        )
        if expr is None:
            return SimpleDesc(reads=[], mutates=[])

        if expr.type == "assignment_expression":
            lhs = expr.child_by_field_name("left")
            rhs = expr.child_by_field_name("right")
            if lhs is not None:
                writes, mutates = _lhs_writes_mutates(lhs)
            else:
                writes, mutates = [], []
            reads: list[str] = []
            # Also read from LHS in compound assignments (e.g. x += y reads x)
            op = None
            for c in expr.children:
                if c.type in ("+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="):
                    op = c.type
                    break
            if op is not None and lhs is not None and lhs.type == "identifier":
                reads.append(_text(lhs))
            if rhs is not None:
                _collect_identifiers(rhs, reads)
            return AssignDesc(
                writes=writes,
                mutates=mutates,
                reads=list(dict.fromkeys(reads)),
            )

        if expr.type == "update_expression":
            # i++ / i-- : reads and mutates i
            for child in expr.named_children:
                if child.type == "identifier":
                    name = _text(child)
                    return AssignDesc(writes=[], mutates=[], reads=[name])
            return SimpleDesc(reads=[], mutates=[])

        if expr.type == "call_expression":
            reads2: list[str] = []
            _collect_identifiers(expr, reads2)
            return SimpleDesc(reads=list(dict.fromkeys(reads2)), mutates=[])

        # Generic fallback
        reads3: list[str] = []
        _collect_identifiers(expr, reads3)
        return SimpleDesc(reads=list(dict.fromkeys(reads3)), mutates=[])
