"""Intra-procedural control-flow graph construction.

For each Function/Method node we walk the body with tree-sitter and emit:

* ``Assignment`` for ``x = expr`` and ``x += expr`` statements
* ``Return`` for ``return [expr]``
* ``Branch`` for each ``if`` / ``elif`` condition, ``except`` clause, and
  ``match`` case
* ``Loop`` for ``for`` and ``while`` headers (``for`` records its target
  names in ``writes``)
* ``Statement`` for everything else — including the ``with`` header, which
  records its ``as`` aliases in ``writes`` and its context expressions in
  ``reads``, and bare mutator method calls (``xs.append(x)``), which record
  the receiver base name in ``mutates``

Edges:

* ``Function -[CONTAINS]-> <cfg-node>``  for every CFG node we emit
* ``<node> -[CONTROLS]-> <next-node>``   for control-flow successors

Topology rules:

* The Function node is the CFG entry — it has outgoing ``CONTROLS`` to the
  first body node.
* ``Return`` is a sink: no outgoing ``CONTROLS``.
* An ``if`` without an ``else`` falls through; the Branch node itself is
  added to the post-branch successor set so the caller wires
  ``Branch -[CONTROLS]-> <after>``.
* A ``Loop`` header has two outgoing successors: the body and the fall-through
  exit. The body's tail nodes get a back-edge to the header.

``with`` / ``try`` / ``match`` bodies are traversed (Sprint 5):

* ``with`` introduces no control dependence — its body keeps the outer
  controller. The header defines the ``as`` aliases.
* ``try`` bodies keep the outer controller (they always start executing).
  Each ``except`` clause becomes a ``Branch`` whose predecessors are the
  try entry *and* the try-body tails (an exception may fire at any point);
  handler bodies are control-dependent on their clause. ``else`` chains off
  the no-exception path; ``finally`` joins every path.
* ``match`` mirrors ``if``/``elif``: one ``Branch`` per case, chained, with
  the last case's Branch left open as the no-match fall-through. Case
  patterns that *bind* names (``case Point(x=a)``) are not extracted.

Inter-procedural CFG, ``break`` / ``continue`` jump targets, and
``for``/``while`` ``else`` clauses are out of scope here — flag and follow
up rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.analyses._python_ast import locate_function, python_parser
from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind


def build(graph: RepoGraph, repo_path: Path) -> None:
    parser = python_parser()
    for func in list(graph.nodes()):
        if func.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        if func.path is None or func.start_line is None:
            continue
        try:
            source = (repo_path / func.path).read_bytes()
        except OSError:
            continue
        tree = parser.parse(source)
        func_ts = locate_function(tree.root_node, func.name, func.start_line - 1)
        if func_ts is None:
            continue
        body = func_ts.child_by_field_name("body")
        if body is None:
            continue
        builder = _CFGBuilder(graph=graph, owner=func, source=source)
        builder.build_block(body, predecessors=[func.id], controller=None)


@dataclass
class _CFGBuilder:
    graph: RepoGraph
    owner: Node
    source: bytes
    _counter: int = field(default=0, init=False)

    def build_block(
        self, block_ts: TSNode, predecessors: list[str], controller: str | None
    ) -> list[str]:
        """Build a straight-line block. Returns the open predecessors at the block's tail."""
        current_preds = list(predecessors)
        for child in block_ts.named_children:
            if child.type == "comment":
                continue
            current_preds = self.build_stmt(child, current_preds, controller)
        return current_preds

    def build_stmt(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        if ts_node.type == "if_statement":
            return self._build_if(ts_node, preds, controller)
        if ts_node.type in {"for_statement", "while_statement"}:
            return self._build_loop(ts_node, preds, controller)
        if ts_node.type == "return_statement":
            return self._build_return(ts_node, preds, controller)
        if ts_node.type == "with_statement":
            return self._build_with(ts_node, preds, controller)
        if ts_node.type == "try_statement":
            return self._build_try(ts_node, preds, controller)
        if ts_node.type == "match_statement":
            return self._build_match(ts_node, preds, controller)
        if ts_node.type == "expression_statement" and _is_assignment(ts_node):
            return self._build_assignment(ts_node, preds, controller)
        return self._emit_simple(ts_node, preds, NodeKind.Statement, "stmt", controller)

    def _emit_simple(
        self,
        ts_node: TSNode,
        preds: list[str],
        kind: NodeKind,
        prefix: str,
        controller: str | None,
    ) -> list[str]:
        node_id = self._new_id(prefix)
        attrs: dict[str, object] = {
            "reads": _extract_reads(ts_node, self.source),
            "mutates": _extract_call_mutations(ts_node, self.source),
            "controlled_by": controller,
        }
        self._add_node(node_id, kind, ts_node, attrs=attrs)
        self._wire(preds, node_id)
        return [node_id]

    def _build_assignment(
        self, ts_node: TSNode, preds: list[str], controller: str | None
    ) -> list[str]:
        node_id = self._new_id("assign")
        writes, mutates = _extract_lhs_targets(ts_node, self.source)
        attrs: dict[str, object] = {
            "writes": writes,
            "mutates": mutates,
            "reads": _extract_reads(ts_node, self.source),
            "controlled_by": controller,
        }
        self._add_node(node_id, NodeKind.Assignment, ts_node, attrs=attrs)
        self._wire(preds, node_id)
        return [node_id]

    def _build_return(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        node_id = self._new_id("return")
        attrs: dict[str, object] = {
            "reads": _extract_reads(ts_node, self.source),
            "controlled_by": controller,
        }
        self._add_node(node_id, NodeKind.Return, ts_node, attrs=attrs)
        self._wire(preds, node_id)
        # Return is a sink — no open successors for the caller to wire.
        return []

    def _build_if(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        branch_id = self._new_id("branch")
        attrs: dict[str, object] = {
            "reads": _extract_reads(ts_node, self.source),
            "controlled_by": controller,
        }
        self._add_node(branch_id, NodeKind.Branch, ts_node, attrs=attrs)
        self._wire(preds, branch_id)

        exits: list[str] = []

        consequence = ts_node.child_by_field_name("consequence")
        if consequence is not None:
            exits.extend(self.build_block(consequence, [branch_id], controller=branch_id))

        alternative = ts_node.child_by_field_name("alternative")
        if alternative is None:
            # No else: the branch itself is an open predecessor for fall-through.
            exits.append(branch_id)
        elif alternative.type == "else_clause":
            else_body = alternative.child_by_field_name("body")
            if else_body is not None:
                exits.extend(self.build_block(else_body, [branch_id], controller=branch_id))
            else:
                exits.append(branch_id)
        elif alternative.type == "elif_clause":
            # Each elif gets its own Branch node, rooted at the parent branch.
            # The elif's *own* outer controller is the parent branch — so the
            # elif's condition is control-dependent on the parent.
            exits.extend(self._build_elif(alternative, [branch_id], controller=branch_id))

        return exits

    def _build_elif(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        branch_id = self._new_id("branch")
        attrs: dict[str, object] = {
            "reads": _extract_reads(ts_node, self.source),
            "controlled_by": controller,
        }
        self._add_node(branch_id, NodeKind.Branch, ts_node, attrs=attrs)
        self._wire(preds, branch_id)

        exits: list[str] = []
        consequence = ts_node.child_by_field_name("consequence")
        if consequence is not None:
            exits.extend(self.build_block(consequence, [branch_id], controller=branch_id))

        alternative = ts_node.child_by_field_name("alternative")
        if alternative is None:
            exits.append(branch_id)
        elif alternative.type == "else_clause":
            else_body = alternative.child_by_field_name("body")
            if else_body is not None:
                exits.extend(self.build_block(else_body, [branch_id], controller=branch_id))
            else:
                exits.append(branch_id)
        elif alternative.type == "elif_clause":
            exits.extend(self._build_elif(alternative, [branch_id], controller=branch_id))

        return exits

    def _build_loop(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        loop_id = self._new_id("loop")
        writes: list[str] = []
        if ts_node.type == "for_statement":
            left = ts_node.child_by_field_name("left")
            if left is not None:
                writes, _ = _split_pattern(left, self.source)
        attrs: dict[str, object] = {
            "writes": writes,
            "reads": _extract_reads(ts_node, self.source),
            "controlled_by": controller,
        }
        self._add_node(loop_id, NodeKind.Loop, ts_node, attrs=attrs)
        self._wire(preds, loop_id)

        body = ts_node.child_by_field_name("body")
        if body is not None:
            body_tail = self.build_block(body, [loop_id], controller=loop_id)
            for tail in body_tail:
                self.graph.add_edge(Edge(src=tail, dst=loop_id, kind=EdgeKind.CONTROLS))

        # Fall-through exit: loop header itself.
        return [loop_id]

    def _build_with(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        header_id = self._new_id("with")
        writes, reads = _with_targets(ts_node, self.source)
        attrs: dict[str, object] = {
            "writes": writes,
            "reads": reads,
            "mutates": [],
            "controlled_by": controller,
        }
        self._add_node(header_id, NodeKind.Statement, ts_node, attrs=attrs)
        self._wire(preds, header_id)

        body = ts_node.child_by_field_name("body")
        if body is None:
            return [header_id]
        # The body always executes: it keeps the *outer* controller.
        return self.build_block(body, [header_id], controller=controller)

    def _build_try(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        body = ts_node.child_by_field_name("body")
        body_tails = self.build_block(body, preds, controller) if body is not None else list(preds)

        no_exception_tails = body_tails
        handler_exits: list[str] = []
        for child in ts_node.named_children:
            if child.type == "except_clause":
                # An exception may fire before any try-body statement completes,
                # so the handler's predecessors include the try entry.
                handler_preds = _dedupe(list(preds) + body_tails)
                handler_exits.extend(self._build_except(child, handler_preds, controller))
            elif child.type == "else_clause":
                else_body = child.child_by_field_name("body")
                if else_body is not None:
                    no_exception_tails = self.build_block(else_body, no_exception_tails, controller)

        exits = _dedupe(no_exception_tails + handler_exits)

        finally_clause = next(
            (c for c in ts_node.named_children if c.type == "finally_clause"), None
        )
        if finally_clause is not None:
            finally_body = next((c for c in finally_clause.children if c.type == "block"), None)
            if finally_body is not None:
                exits = self.build_block(finally_body, exits, controller)
        return exits

    def _build_except(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        branch_id = self._new_id("except")
        writes: list[str] = []
        value = ts_node.child_by_field_name("value")
        if value is not None and value.type == "as_pattern":
            alias = value.child_by_field_name("alias")
            if alias is not None and alias.named_children:
                writes, _ = _split_pattern(alias.named_children[0], self.source)
        attrs: dict[str, object] = {
            "writes": writes,
            "reads": [],
            "controlled_by": controller,
        }
        self._add_node(branch_id, NodeKind.Branch, ts_node, attrs=attrs)
        self._wire(preds, branch_id)

        block = next((c for c in ts_node.children if c.type == "block"), None)
        if block is None:
            return [branch_id]
        return self.build_block(block, [branch_id], controller=branch_id)

    def _build_match(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        subject = ts_node.child_by_field_name("subject")
        subject_reads: list[str] = []
        if subject is not None:
            seen: set[str] = set()
            _collect_reads(subject, self.source, subject_reads, seen)

        body = ts_node.child_by_field_name("body")
        case_clauses = (
            [c for c in body.named_children if c.type == "case_clause"] if body is not None else []
        )
        if not case_clauses:
            return self._emit_simple(ts_node, preds, NodeKind.Statement, "stmt", controller)

        exits: list[str] = []
        current_preds = list(preds)
        current_controller = controller
        last_branch: str | None = None
        for case in case_clauses:
            branch_id = self._new_id("case")
            reads = list(subject_reads)
            guard = case.child_by_field_name("guard")
            if guard is not None:
                seen_g: set[str] = set(reads)
                _collect_reads(guard, self.source, reads, seen_g)
            attrs: dict[str, object] = {
                "reads": reads,
                "controlled_by": current_controller,
            }
            self._add_node(branch_id, NodeKind.Branch, case, attrs=attrs)
            self._wire(current_preds, branch_id)

            consequence = case.child_by_field_name("consequence")
            if consequence is not None:
                exits.extend(self.build_block(consequence, [branch_id], controller=branch_id))

            current_preds = [branch_id]
            current_controller = branch_id
            last_branch = branch_id

        if last_branch is not None:
            # No case matched: fall through past the last case's Branch.
            exits.append(last_branch)
        return _dedupe(exits)

    def _add_node(
        self,
        node_id: str,
        kind: NodeKind,
        ts_node: TSNode,
        attrs: dict[str, object] | None = None,
    ) -> None:
        self.graph.add_node(
            Node(
                id=node_id,
                kind=kind,
                name=_short_name(ts_node, self.source),
                path=self.owner.path,
                start_line=ts_node.start_point[0] + 1,
                end_line=ts_node.end_point[0] + 1,
                attrs=dict(attrs) if attrs else {},
            )
        )
        self.graph.add_edge(Edge(src=self.owner.id, dst=node_id, kind=EdgeKind.CONTAINS))

    def _wire(self, preds: list[str], dst: str) -> None:
        for pred in preds:
            self.graph.add_edge(Edge(src=pred, dst=dst, kind=EdgeKind.CONTROLS))

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}:{self.owner.id}#{self._counter}"


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
    }
)


def _dedupe(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _is_assignment(expr_stmt: TSNode) -> bool:
    return any(child.type in _ASSIGNMENT_TYPES for child in expr_stmt.children)


def _extract_lhs_targets(expr_stmt: TSNode, source: bytes) -> tuple[list[str], list[str]]:
    """Split an assignment's LHS into (writes, mutates).

    * ``writes`` — names *bound* by this statement (identifier LHS, recursed
      through tuple/list patterns).
    * ``mutates`` — base names of attribute/subscript LHS targets (these
      mutate existing objects but don't introduce a new binding).

    ``self.x = 1`` records ``mutates=["self"]``; ``xs[0] = 1`` records
    ``mutates=["xs"]``; ``x, obj.y = ...`` records ``writes=["x"]`` and
    ``mutates=["obj"]``. Augmented assignments (``x += 1``,
    ``self.total += n``) follow the same LHS rules.
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


# ---- reads extraction (drives PDG data dependence) ---------------------------


def _extract_reads(stmt_ts: TSNode, source: bytes) -> list[str]:
    """Identifier names read as data by this statement.

    Per stmt kind we pick the right sub-expression (RHS / condition /
    iterable / returned value / generic). Attribute names and called
    function names are excluded — only data identifiers count.

    Augmented assignments read both sides: ``x += y`` reads ``x`` and ``y``;
    ``self.total += n`` reads ``self`` (attribute base) and ``n``.
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
    """(writes, reads) for a ``with`` header.

    Each ``with_item`` contributes its context expression's data reads;
    an ``as`` alias contributes a write.
    """
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
    """Receiver base names mutated by a bare mutator method call.

    ``xs.append(x)`` returns ``["xs"]``; ``self.config.update(d)`` returns
    ``["self"]``. Only statement-level calls whose method name is in
    :data:`_MUTATOR_METHODS` count — a heuristic: it misses unknown mutator
    names and ``x = xs.pop()`` (call in an assignment RHS).
    """
    if stmt_ts.type != "expression_statement":
        return []
    for child in stmt_ts.named_children:
        if child.type != "call":
            continue
        fn = child.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        if attr is None or obj is None:
            continue
        if _text(attr, source) in _MUTATOR_METHODS:
            return _base_names(obj, source)
    return []


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


def _text(ts_node: TSNode, source: bytes) -> str:
    return source[ts_node.start_byte : ts_node.end_byte].decode("utf-8", errors="replace")


def _short_name(ts_node: TSNode, source: bytes) -> str:
    raw = source[ts_node.start_byte : ts_node.end_byte].decode("utf-8", errors="replace")
    first_line = raw.splitlines()[0] if raw else ""
    return first_line.strip()[:80]
