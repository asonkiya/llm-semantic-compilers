"""Intra-procedural control-flow graph construction.

For each Function/Method node we walk the body with tree-sitter and emit:

* ``Assignment`` for ``x = expr`` statements
* ``Return`` for ``return [expr]``
* ``Branch`` for each ``if`` / ``elif`` condition
* ``Loop`` for ``for`` and ``while`` headers
* ``Statement`` for everything else (pass, break, continue, with, raise, ...)

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

Inter-procedural CFG, ``try``/``except`` flow, ``match``, ``break`` /
``continue`` jump targets, and ``for``/``while`` ``else`` clauses are out of
scope here — flag and follow up rather than guess.
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
        attrs: dict[str, object] = {
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


def _is_assignment(expr_stmt: TSNode) -> bool:
    return any(child.type == "assignment" for child in expr_stmt.children)


def _extract_lhs_targets(expr_stmt: TSNode, source: bytes) -> tuple[list[str], list[str]]:
    """Split an assignment's LHS into (writes, mutates).

    * ``writes`` — names *bound* by this statement (identifier LHS, recursed
      through tuple/list patterns).
    * ``mutates`` — base names of attribute/subscript LHS targets (these
      mutate existing objects but don't introduce a new binding).

    ``self.x = 1`` records ``mutates=["self"]``; ``xs[0] = 1`` records
    ``mutates=["xs"]``; ``x, obj.y = ...`` records ``writes=["x"]`` and
    ``mutates=["obj"]``.
    """
    for child in expr_stmt.children:
        if child.type == "assignment":
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
    """
    target = _read_target(stmt_ts)
    if target is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    _collect_reads(target, source, names, seen)
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


def _text(ts_node: TSNode, source: bytes) -> str:
    return source[ts_node.start_byte : ts_node.end_byte].decode("utf-8", errors="replace")


def _short_name(ts_node: TSNode, source: bytes) -> str:
    raw = source[ts_node.start_byte : ts_node.end_byte].decode("utf-8", errors="replace")
    first_line = raw.splitlines()[0] if raw else ""
    return first_line.strip()[:80]
