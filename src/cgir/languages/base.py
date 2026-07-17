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

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from tree_sitter import Node as TSNode

# Bump when the LanguageAdapter surface changes incompatibly. New optional
# methods get base-class defaults (see direct_effects_confidence,
# global_declared_names), so most evolution does NOT require a bump —
# plugins declaring an older version load with a warning.
ADAPTER_API_VERSION = 1

_PIN_RE = re.compile(r"cgir:\s*([a-z0-9_,\- ]+)", re.IGNORECASE)

# Grammars disagree on the comment node type: python/ts/go use "comment",
# rust/c/c++/java use line_comment/block_comment. (Found by the docs-only
# Rust-adapter experiment — pins silently never appeared.)
COMMENT_NODE_TYPES = frozenset({"comment", "line_comment", "block_comment", "doc_comment"})


class PinIndex:
    """Row-indexed ``cgir:`` pragmas from a file's comment nodes.

    Grammar-agnostic: both supported grammars use ``comment`` nodes, so the
    index collects every comment by row and adapters ask for the pins that
    belong to a definition (trailing comment on its first row + the
    contiguous comment block directly above) or to the module (the header
    comment block, when not directly attached to the first definition).
    """

    def __init__(self, root: TSNode, source: bytes) -> None:
        self._pins_by_row: dict[int, list[str]] = {}
        self._comment_rows: set[int] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in COMMENT_NODE_TYPES:
                row = node.start_point[0]
                self._comment_rows.add(row)
                text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
                match = _PIN_RE.search(text)
                if match:
                    tokens = [t for t in re.split(r"[,\s]+", match.group(1).strip()) if t]
                    self._pins_by_row.setdefault(row, []).extend(tokens)
            stack.extend(node.children)

    def for_definition(self, outermost: TSNode) -> list[str]:
        """Pins attached to a definition (pass the *outermost* node, so a
        pin above a decorator or ``export`` keyword is found)."""
        start = outermost.start_point[0]
        pins = list(self._pins_by_row.get(start, []))  # trailing form
        row = start - 1
        while row in self._comment_rows:
            pins.extend(self._pins_by_row.get(row, []))
            row -= 1
        return sorted(set(pins))

    def module_pins(self, first_decl_row: int | None) -> list[str]:
        """Pins from the file-header comment block (rows from 0), unless that
        block is directly attached to the first definition."""
        if 0 not in self._comment_rows:
            return []
        row, last = 0, -1
        pins: list[str] = []
        while row in self._comment_rows:
            pins.extend(self._pins_by_row.get(row, []))
            last = row
            row += 1
        if first_decl_row is not None and last == first_decl_row - 1:
            return []  # header block belongs to the first definition
        return sorted(set(pins))


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


# --- normalized module declarations (phase 3: ingest) ---------------------------
#
# The structural spine (Repository → File → Module → members + CONTAINS) and
# node/edge construction are language-universal and live in
# sources/tree_sitter_source.py. The adapter walks a module root and yields
# these; the ingester never looks at grammar node types.


@dataclass(slots=True)
class ParamDecl:
    name: str
    node: TSNode


@dataclass(slots=True)
class FunctionDecl:
    node: TSNode
    name: str
    params: list[ParamDecl] = field(default_factory=list)
    signature: str = ""
    returns: str | None = None
    doc: str = ""
    raises: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    free_names: list[str] = field(default_factory=list)
    # developer-declared invariants from ``cgir:`` comment pragmas
    pins: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ClassDecl:
    node: TSNode
    name: str
    methods: list[FunctionDecl] = field(default_factory=list)
    # field name → declared type name, for DI/receiver call resolution
    # (``this.svc.method()`` where ``svc: ChaptersService``).
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ImportDecl:
    node: TSNode
    target: str
    alias: str | None = None


@dataclass(slots=True)
class VariableDecl:
    node: TSNode
    name: str


Declaration = FunctionDecl | ClassDecl | ImportDecl | VariableDecl


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

    def function_index_entries(self, root: TSNode, source: bytes):
        """Yield ``(name, start_row, node)`` for every function/method
        definition in one tree walk. Powers :meth:`SourceCache.locate`'s
        per-file index — O(tree) once instead of O(functions x tree)
        (the hot path found scanning SQLite's 270k-line amalgamation).
        Default: empty — the cache falls back to :meth:`locate_function`."""
        return iter(())

    def direct_effects_confidence(
        self, func_node: TSNode, source: bytes, aliases: dict[str, str]
    ) -> dict[str, str]:
        """Effect tags with provenance: ``high`` (exact/prefix table match)
        vs ``lexical`` (suffix / receiver-name heuristics). Default: every
        tag from :meth:`direct_effects` is high."""
        return dict.fromkeys(self.direct_effects(func_node, source, aliases), "high")

    def global_declared_names(self, func_node: TSNode, source: bytes) -> set[str]:
        """Names this function declares as outer-scope (`global`/`nonlocal`).

        Assignments to these names mutate state *outside* the function, so
        the CFG builder records them as ``mutates`` rather than local
        ``writes``. Default: none (TS/Go have no such declaration form)."""
        return set()

    # --- phase 3: ingest extraction ------------------------------------------

    @abstractmethod
    def module_declarations(
        self, root: TSNode, source: bytes, module_name: str, rel_path: str
    ) -> list[Declaration]:
        """Top-level declarations of a module, fully extracted.

        ``module_name`` is the dotted module path (for languages with dotted
        relative imports, e.g. Python ``from ..a import x``); ``rel_path`` is
        the repo-relative file path (for path-specifier imports, e.g. TS
        ``from './util'``). Method params exclude implicit receivers
        (``self``/``this``).
        """
