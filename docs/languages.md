# Adding a language

CGIR's analysis pipeline (symbols → call graph → CFG → reaching-defs → PDG
→ param-flow → effects → purity → slice) is written **once** against the
`RepoGraph` and a fixed node-attr contract. Per-language differences live
behind a single seam: `LanguageAdapter` (`cgir/languages/base.py`).

Add a language = write one adapter + register it. Everything downstream
(`stats`, `viz`, `flow`, `diff`, `pack`, `lint`, `verify`, MCP) then works
on it.

## The seam

Tree-sitter is the shared substrate — Python, TypeScript, Go, Rust all have
grammars — so adapters trade in `tree_sitter.Node`. The adapter abstracts
the *grammar* (node-type strings, field names, stdlib effect tables), not
the parser technology.

## Migration status (grammar-agnostic refactor)

The pipeline is being moved behind the adapter in phases; each keeps the
full test suite green.

| phase | scope | status |
|---|---|---|
| 1 | parse / locate / **effects** / **call sites** | ✅ done |
| 2 | CFG statement classification + field extraction (normalized `StatementDesc`) | ✅ done |
| 3 | ingest structural dispatch + attr extraction (normalized `Declaration`s) | ✅ done |
| 4 | `TypeScriptAdapter` + per-file language dispatch | ✅ done |
| 5 | `GoAdapter` (package=directory merge, struct-field DI, panic≙raise) | ✅ done |

**Two languages ship (Python, TypeScript); a mixed repo scans both.**
Zero grammar node-type strings remain outside `cgir/languages/`. The CFG builder is pure topology over statement
descriptors; the ingester is pure graph construction over declarations.
Refactor validated end-to-end: rescanning a 441-component repo pre/post
refactor and running `cgir diff` reports **no changes**.

## Writing an adapter (current surface)

```python
from cgir.languages.base import LanguageAdapter

class MyLangAdapter(LanguageAdapter):
    name = "mylang"
    file_extensions = (".ml",)

    # phase 1 — parsing + effects + calls
    def parse(self, source): ...                    # -> tree root node
    def locate_function(self, root, name, row): ...
    def direct_effects(self, fn, source, aliases): ...  # {io,net,fs,db,...}
    def call_sites(self, fn, source): ...           # [(callee, args, line)]

    # phase 2 — CFG extraction (normalized StatementDesc union)
    def function_body(self, fn): ...
    def block_statements(self, block): ...
    def describe_statement(self, node, source): ...  # Branch/Loop/Try/... desc

    # phase 3 — ingest extraction (normalized Declarations)
    def module_declarations(self, root, source, module_name): ...
```

The topology/algorithms you do **not** write per language: CFG wiring,
reaching definitions, PDG, param-flow, purity, the transitive effect
closure, symbol-table resolution, slicing, and all seven product surfaces.

Register it in `cgir/languages/__init__.py:ADAPTERS`. The effect *taxonomy*
(`io`/`net`/`fs`/`db`/`nondeterm`/`raise`) is language-neutral and fixed in
`analyses/effects.py`; the adapter only decides which calls map to which tag.
