"""LanguageAdapter — the per-language seam.

Every downstream analysis (symbols, cfg, effects, pdg, param-flow, purity,
slicing) is written *once* against the RepoGraph and a shared node-attr
contract. What differs between languages is only grammar-specific
extraction: what node type is a call, which builtins are effectful, how a
branch's condition is reached. A ``LanguageAdapter`` answers exactly those
questions, so a new language is one adapter, not a re-implemented pipeline.

Tree-sitter is the shared substrate (Python, TypeScript, Go, Rust all have
grammars), so adapters trade in :class:`tree_sitter.Node`. The adapter
abstracts the *grammar*, not the parser technology.

The surface grows by phase as passes are migrated behind it:
* phase 1 — parse / locate / effects / calls
* phase 2 — CFG statement classification + field extraction
* phase 3 — ingest structural dispatch + attr extraction
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from tree_sitter import Node as TSNode

# (dotted_callee, arg_identifier_names, 1-based_line)
CallSite = tuple[str, list[str], int]


# --- normalized statement descriptors (phase 2: CFG) ---------------------------
#
# The CFG *topology* (how branches wire, loop back-edges, try/finally joins)
# is language-universal and lives in cgir/analyses/cfg.py. The adapter's job
# is to classify each statement and hand back its parts in one of these
# shapes; the builder never looks at grammar node types.


@dataclass(slots=True)
class SimpleDesc:
    reads: list[str] = field(default_factory=list)
    mutates: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AssignDesc:
    writes: list[str] = field(default_factory=list)
    mutates: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReturnDesc:
    reads: list[str] = field(default_factory=list)
    mutates: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BranchDesc:
    """An if/elif (or `else if`) arm. Chains via ``next_branch``."""

    reads: list[str] = field(default_factory=list)
    consequence: TSNode | None = None
    else_block: TSNode | None = None
    next_branch: TSNode | None = None  # elif_clause / nested `if` — described again


@dataclass(slots=True)
class LoopDesc:
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)  # for-targets
    body: TSNode | None = None


@dataclass(slots=True)
class WithDesc:
    """Resource-acquisition header (with / using / try-with-resources)."""

    writes: list[str] = field(default_factory=list)  # `as` aliases
    reads: list[str] = field(default_factory=list)
    body: TSNode | None = None


@dataclass(slots=True)
class HandlerDesc:
    node: TSNode
    writes: list[str] = field(default_factory=list)  # `except ... as e`
    block: TSNode | None = None


@dataclass(slots=True)
class TryDesc:
    body: TSNode | None = None
    handlers: list[HandlerDesc] = field(default_factory=list)
    else_block: TSNode | None = None
    finally_block: TSNode | None = None


@dataclass(slots=True)
class CaseDesc:
    node: TSNode
    reads: list[str] = field(default_factory=list)  # subject + guard
    consequence: TSNode | None = None


@dataclass(slots=True)
class MatchDesc:
    cases: list[CaseDesc] = field(default_factory=list)


StatementDesc = (
    SimpleDesc | AssignDesc | ReturnDesc | BranchDesc | LoopDesc | WithDesc | TryDesc | MatchDesc
)


class LanguageAdapter(ABC):
    name: str = "base"
    file_extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, source: bytes) -> TSNode:
        """Parse source bytes; return the tree's root node."""

    @abstractmethod
    def locate_function(self, root: TSNode, name: str, start_row: int) -> TSNode | None:
        """The function/method node named ``name`` starting on 0-based ``start_row``."""

    @abstractmethod
    def direct_effects(self, func_node: TSNode, source: bytes, aliases: dict[str, str]) -> set[str]:
        """Direct effect tags (io/net/fs/db/nondeterm/raise) in the body.

        ``aliases`` maps local import names to their absolute dotted target
        (built language-neutrally from ``Import`` nodes), so an adapter can
        resolve ``r.get`` → ``requests.get`` before matching its tables.
        """

    @abstractmethod
    def call_sites(self, func_node: TSNode, source: bytes) -> list[CallSite]:
        """Call sites in the body: dotted callee, arg identifier names, line."""

    # --- phase 2: CFG extraction --------------------------------------------

    @abstractmethod
    def function_body(self, func_node: TSNode) -> TSNode | None:
        """The function's body block node."""

    @abstractmethod
    def block_statements(self, block: TSNode) -> list[TSNode]:
        """Statement nodes of a block, comments filtered."""

    @abstractmethod
    def describe_statement(self, node: TSNode, source: bytes) -> StatementDesc:
        """Classify one statement and extract its parts (see descriptors)."""
