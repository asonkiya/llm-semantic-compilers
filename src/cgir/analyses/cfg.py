"""Intra-procedural control-flow graph construction — language-neutral.

For each Function/Method node we walk the body via the active
:class:`~cgir.languages.LanguageAdapter` and emit:

* ``Assignment`` for binding statements (``x = e``, ``x += e``)
* ``Return`` for returns (CFG sinks)
* ``Branch`` for each conditional arm, exception handler, and match case
* ``Loop`` for loop headers (for-targets recorded in ``writes``)
* ``Statement`` for everything else — including resource headers (``with``),
  which record their aliases in ``writes``, and mutator calls in ``mutates``

Edges:

* ``Function -[CONTAINS]-> <cfg-node>``  for every CFG node we emit
* ``<node> -[CONTROLS]-> <next-node>``   for control-flow successors

The *topology* rules here are language-universal (branch fall-through, loop
back-edges, try/finally joins, case chains); everything grammar-specific —
what node type is a loop, where its condition lives, which names it binds —
comes from the adapter as a normalized
:class:`~cgir.languages.base.StatementDesc`:

* The Function node is the CFG entry; ``Return`` is a sink.
* A branch without an else falls through (the Branch node joins the
  post-branch successor set); else-if arms chain as nested Branch nodes,
  each control-dependent on its parent.
* A ``Loop`` header has two successors (body, fall-through); body tails
  get a back-edge to the header.
* Resource headers (``with``) introduce no control dependence — the body
  keeps the outer controller.
* ``try`` bodies keep the outer controller; each handler is a ``Branch``
  whose predecessors include the try entry *and* the body tails; ``else``
  chains off the no-exception path; ``finally`` joins every path.
* ``match``/``switch`` cases chain like else-if arms, with the last case's
  Branch left open as the no-match fall-through.

``break``/``continue`` jump targets and loop ``else`` clauses are out of
scope — flag rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.languages import LanguageAdapter, SourceCache
from cgir.languages.base import (
    AssignDesc,
    BranchDesc,
    LoopDesc,
    MatchDesc,
    ReturnDesc,
    SimpleDesc,
    TryDesc,
    WithDesc,
)


def build(graph: RepoGraph, repo_path: Path, adapter: LanguageAdapter | None = None) -> None:
    cache = SourceCache(repo_path, adapter)
    for func in list(graph.nodes()):
        if func.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        if func.path is None or func.start_line is None:
            continue
        parsed = cache.get(func.path)
        if parsed is None:
            continue
        source, root, file_adapter = parsed
        func_ts = file_adapter.locate_function(root, func.name, func.start_line - 1)
        if func_ts is None:
            continue
        body = file_adapter.function_body(func_ts)
        if body is None:
            continue
        builder = _CFGBuilder(
            graph=graph,
            owner=func,
            source=source,
            adapter=file_adapter,
            global_names=file_adapter.global_declared_names(func_ts, source),
        )
        builder.build_block(body, predecessors=[func.id], controller=None)


@dataclass
class _CFGBuilder:
    graph: RepoGraph
    owner: Node
    source: bytes
    adapter: LanguageAdapter
    # names declared global/nonlocal in this function: writes to them mutate
    # outer state and are recorded as `mutates`, not local `writes`.
    global_names: set[str] = field(default_factory=set)
    _counter: int = field(default=0, init=False)

    def build_block(
        self, block_ts: TSNode, predecessors: list[str], controller: str | None
    ) -> list[str]:
        """Build a straight-line block. Returns the open predecessors at the block's tail."""
        current_preds = list(predecessors)
        for child in self.adapter.block_statements(block_ts):
            current_preds = self.build_stmt(child, current_preds, controller)
        return current_preds

    def build_stmt(self, ts_node: TSNode, preds: list[str], controller: str | None) -> list[str]:
        desc = self.adapter.describe_statement(ts_node, self.source)
        if isinstance(desc, AssignDesc) and self.global_names:
            outer = [w for w in desc.writes if w in self.global_names]
            if outer:
                desc.writes = [w for w in desc.writes if w not in self.global_names]
                desc.mutates = list(desc.mutates) + outer
        if isinstance(desc, BranchDesc):
            return self._build_branch(ts_node, desc, preds, controller)
        if isinstance(desc, LoopDesc):
            return self._build_loop(ts_node, desc, preds, controller)
        if isinstance(desc, ReturnDesc):
            return self._build_return(ts_node, desc, preds, controller)
        if isinstance(desc, WithDesc):
            return self._build_with(ts_node, desc, preds, controller)
        if isinstance(desc, TryDesc):
            return self._build_try(desc, preds, controller)
        if isinstance(desc, MatchDesc):
            return self._build_match(desc, preds, controller)
        if isinstance(desc, AssignDesc):
            return self._build_assignment(ts_node, desc, preds, controller)
        return self._emit_simple(ts_node, desc, preds, controller)

    def _emit_simple(
        self, ts_node: TSNode, desc: SimpleDesc, preds: list[str], controller: str | None
    ) -> list[str]:
        node_id = self._new_id("stmt")
        attrs: dict[str, object] = {
            "reads": desc.reads,
            "mutates": desc.mutates,
            "controlled_by": controller,
        }
        self._add_node(node_id, NodeKind.Statement, ts_node, attrs=attrs)
        self._wire(preds, node_id)
        return [node_id]

    def _build_assignment(
        self, ts_node: TSNode, desc: AssignDesc, preds: list[str], controller: str | None
    ) -> list[str]:
        node_id = self._new_id("assign")
        attrs: dict[str, object] = {
            "writes": desc.writes,
            "mutates": desc.mutates,
            "reads": desc.reads,
            "controlled_by": controller,
        }
        self._add_node(node_id, NodeKind.Assignment, ts_node, attrs=attrs)
        self._wire(preds, node_id)
        return [node_id]

    def _build_return(
        self, ts_node: TSNode, desc: ReturnDesc, preds: list[str], controller: str | None
    ) -> list[str]:
        node_id = self._new_id("return")
        attrs: dict[str, object] = {
            "reads": desc.reads,
            "mutates": desc.mutates,
            "controlled_by": controller,
        }
        self._add_node(node_id, NodeKind.Return, ts_node, attrs=attrs)
        self._wire(preds, node_id)
        # Return is a sink — no open successors for the caller to wire.
        return []

    def _build_branch(
        self, ts_node: TSNode, desc: BranchDesc, preds: list[str], controller: str | None
    ) -> list[str]:
        branch_id = self._new_id("branch")
        attrs: dict[str, object] = {"reads": desc.reads, "controlled_by": controller}
        self._add_node(branch_id, NodeKind.Branch, ts_node, attrs=attrs)
        self._wire(preds, branch_id)

        exits: list[str] = []
        if desc.consequence is not None:
            exits.extend(self.build_block(desc.consequence, [branch_id], controller=branch_id))

        if desc.next_branch is not None:
            # else-if arm: its own Branch, control-dependent on this one.
            next_desc = self.adapter.describe_statement(desc.next_branch, self.source)
            if isinstance(next_desc, BranchDesc):
                exits.extend(
                    self._build_branch(desc.next_branch, next_desc, [branch_id], branch_id)
                )
            else:  # defensive: treat unexpected shapes as an else block
                exits.extend(self.build_stmt(desc.next_branch, [branch_id], branch_id))
        elif desc.else_block is not None:
            exits.extend(self.build_block(desc.else_block, [branch_id], controller=branch_id))
        else:
            # No else: the branch itself is an open predecessor for fall-through.
            exits.append(branch_id)
        return exits

    def _build_loop(
        self, ts_node: TSNode, desc: LoopDesc, preds: list[str], controller: str | None
    ) -> list[str]:
        loop_id = self._new_id("loop")
        attrs: dict[str, object] = {
            "writes": desc.writes,
            "reads": desc.reads,
            "controlled_by": controller,
        }
        self._add_node(loop_id, NodeKind.Loop, ts_node, attrs=attrs)
        self._wire(preds, loop_id)

        if desc.body is not None:
            body_tail = self.build_block(desc.body, [loop_id], controller=loop_id)
            for tail in body_tail:
                self.graph.add_edge(Edge(src=tail, dst=loop_id, kind=EdgeKind.CONTROLS))

        # Fall-through exit: loop header itself.
        return [loop_id]

    def _build_with(
        self, ts_node: TSNode, desc: WithDesc, preds: list[str], controller: str | None
    ) -> list[str]:
        header_id = self._new_id("with")
        attrs: dict[str, object] = {
            "writes": desc.writes,
            "reads": desc.reads,
            "mutates": [],
            "controlled_by": controller,
        }
        self._add_node(header_id, NodeKind.Statement, ts_node, attrs=attrs)
        self._wire(preds, header_id)

        if desc.body is None:
            return [header_id]
        # The body always executes: it keeps the *outer* controller.
        return self.build_block(desc.body, [header_id], controller=controller)

    def _build_try(self, desc: TryDesc, preds: list[str], controller: str | None) -> list[str]:
        body_tails = (
            self.build_block(desc.body, preds, controller) if desc.body is not None else list(preds)
        )

        no_exception_tails = body_tails
        handler_exits: list[str] = []
        for handler in desc.handlers:
            # An exception may fire before any try-body statement completes,
            # so the handler's predecessors include the try entry.
            handler_preds = _dedupe(list(preds) + body_tails)
            branch_id = self._new_id("except")
            attrs: dict[str, object] = {
                "writes": handler.writes,
                "reads": [],
                "controlled_by": controller,
            }
            self._add_node(branch_id, NodeKind.Branch, handler.node, attrs=attrs)
            self._wire(handler_preds, branch_id)
            if handler.block is not None:
                handler_exits.extend(self.build_block(handler.block, [branch_id], branch_id))
            else:
                handler_exits.append(branch_id)

        if desc.else_block is not None:
            no_exception_tails = self.build_block(desc.else_block, no_exception_tails, controller)

        exits = _dedupe(no_exception_tails + handler_exits)

        if desc.finally_block is not None:
            exits = self.build_block(desc.finally_block, exits, controller)
        return exits

    def _build_match(self, desc: MatchDesc, preds: list[str], controller: str | None) -> list[str]:
        exits: list[str] = []
        current_preds = list(preds)
        current_controller = controller
        last_branch: str | None = None
        for case in desc.cases:
            branch_id = self._new_id("case")
            attrs: dict[str, object] = {"reads": case.reads, "controlled_by": current_controller}
            self._add_node(branch_id, NodeKind.Branch, case.node, attrs=attrs)
            self._wire(current_preds, branch_id)

            if case.consequence is not None:
                exits.extend(self.build_block(case.consequence, [branch_id], branch_id))

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


def _dedupe(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _short_name(ts_node: TSNode, source: bytes) -> str:
    raw = source[ts_node.start_byte : ts_node.end_byte].decode("utf-8", errors="replace")
    first_line = raw.splitlines()[0] if raw else ""
    return first_line.strip()[:80]
