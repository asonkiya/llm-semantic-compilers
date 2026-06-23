"""Shared tree-sitter helpers for the Python-target analyses.

Centralizes the parser instance + the function-locator that every analysis
pass needs when it has to walk a function body. Lives behind a leading
underscore because it's an internal seam — the long-term direction is
to push this work down into ``GraphSource`` so analyses become pure-graph
readers (see ``docs/roadmap.md`` "Grammar-agnostic core refactor").
"""

from __future__ import annotations

import tree_sitter_python
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode


def python_parser() -> Parser:
    language = Language(tree_sitter_python.language())
    parser = Parser()
    parser.language = language
    return parser


def locate_function(root: TSNode, name: str, start_row: int) -> TSNode | None:
    """Find a ``function_definition`` whose name matches and starts on ``start_row``."""
    stack: list[TSNode] = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition" and node.start_point[0] == start_row:
            name_node = node.child_by_field_name("name")
            if (
                name_node is not None
                and name_node.text is not None
                and name_node.text.decode("utf-8", errors="replace") == name
            ):
                return node
        stack.extend(node.children)
    return None
