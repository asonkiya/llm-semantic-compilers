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
| 2 | CFG statement classification + field extraction | pending |
| 3 | ingest structural dispatch + attr extraction (signature, doc, raises, free-names, imports) | pending |
| 4 | `TypeScriptAdapter` + language selection in the pipeline | pending |

Until phases 2–3 land, `cfg.py` and `tree_sitter_source.py` still hardcode
Python grammar; a second language is fully plug-and-play only after those.

## Writing an adapter (current surface)

```python
from cgir.languages.base import LanguageAdapter

class MyLangAdapter(LanguageAdapter):
    name = "mylang"
    file_extensions = (".ml",)

    def parse(self, source: bytes): ...            # -> tree root node
    def locate_function(self, root, name, row): ... # find fn by name+row
    def direct_effects(self, fn, source, aliases): ...  # {io,net,fs,db,...}
    def call_sites(self, fn, source): ...           # [(callee, args, line)]
```

Register it in `cgir/languages/__init__.py:ADAPTERS`. The effect *taxonomy*
(`io`/`net`/`fs`/`db`/`nondeterm`/`raise`) is language-neutral and fixed in
`analyses/effects.py`; the adapter only decides which calls map to which tag.
