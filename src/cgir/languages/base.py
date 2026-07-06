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

from tree_sitter import Node as TSNode

# (dotted_callee, arg_identifier_names, 1-based_line)
CallSite = tuple[str, list[str], int]


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
