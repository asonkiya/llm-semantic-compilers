# Writing a language adapter

This document is **self-contained**: everything needed to implement a new
language for CGIR without reading its source. An adapter is one class
implementing ~8 methods over a tree-sitter grammar; every downstream
feature — effects, purity, kind classification, pins, packs, blast radius,
the pre-commit gate, the LSP — works on your language for free.

## The mental model

CGIR's pipeline is language-neutral. Only *grammar extraction* is per
language, behind the `LanguageAdapter` ABC:

1. **Ingest** calls `module_declarations` to get normalized declarations
   (functions, classes, imports, variables) and builds the graph.
2. **Call graph** calls `call_sites` per function and resolves callees via
   language-neutral symbol tables.
3. **CFG** calls `function_body` / `block_statements` /
   `describe_statement` per function; the *topology* (branch wiring, loop
   back-edges) is built centrally — you only classify statements.
4. **Effects** calls `direct_effects_confidence` per function; transitive
   propagation is central.

You never build graph nodes or edges. You translate grammar shapes into
the descriptor dataclasses below.

## Grammar version compatibility (check first)

tree-sitter grammar wheels ship a compiled language ABI; the `tree-sitter`
core only accepts a range. If `Language(...)` raises
``Incompatible Language version``, pin an older grammar wheel (e.g.
``tree-sitter-rust<0.24`` against ``tree-sitter 0.24``).

## Setup

```python
import tree_sitter_rust  # any tree-sitter grammar package
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cgir.languages.base import (
    ADAPTER_API_VERSION, AssignDesc, BranchDesc, CallSite, CaseDesc,
    ClassDecl, Declaration, FunctionDecl, ImportDecl, LanguageAdapter,
    LoopDesc, MatchDesc, ParamDecl, PinIndex, ReturnDesc, SimpleDesc,
    StatementDesc, TryDesc, VariableDecl, WithDesc,
)

class RustAdapter(LanguageAdapter):
    name = "rust"                      # unique language name
    file_extensions = (".rs",)         # extensions this adapter claims
    api_version = ADAPTER_API_VERSION  # currently 1

    def __init__(self) -> None:
        self._parser = Parser(Language(tree_sitter_rust.language()))
```

Useful helper you'll write once: `node.text` returns the node's source
bytes (offset-safe); decode with `errors="replace"`.

**Comment node types:** grammars disagree (`comment` in python/ts/go;
`line_comment`/`block_comment` in rust/c/java). `PinIndex` handles all of
these (`cgir.languages.base.COMMENT_NODE_TYPES`) — but your
`block_statements` comment filter must use *your grammar's* comment types.

## The required methods, exactly

### `parse(self, source: bytes) -> TSNode`
Return `self._parser.parse(source).root_node`.

### `locate_function(self, root, name: str, start_row: int) -> TSNode | None`
Find the function/method definition node whose **name** matches and whose
**start row equals `start_row` (0-based)**. Analyses call this to re-find a
function from graph metadata; if it returns None for a function, no
analysis runs on it. Walk the whole tree (functions nest inside classes/
impl blocks).

### `module_declarations(self, root, source: bytes, module_name: str, rel_path: str) -> list[Declaration]`
Walk the module's top level and return normalized declarations.
`module_name` is the dotted module path derived from the file path
(`internal/store/keys.rs` → `internal.store.keys`); `rel_path` is the
repo-relative file path (for path-based import specifiers).

Declaration shapes (all carry `node: TSNode` — used for line spans):

- **`FunctionDecl(node, name, params, signature, returns, doc, raises,
  decorators, free_names, pins)`**
  - `params`: `list[ParamDecl(name, node)]` — **exclude** the implicit
    receiver (`self`/`this`).
  - `signature`: human-readable, e.g. `"add(a: i64, b: i64) -> i64"`.
  - `returns`: return-type text or None.
  - `doc`: docstring/doc-comment text, `""` if none.
  - `raises`: exception-ish names; for panic-style languages return
    `["panic"]` when the body contains a panic, else `[]`. For
    Result/error-value languages (Rust `?`, Go `error` returns), error
    *values* are not raises — only aborts (panic) count.
  - `decorators`: attribute/annotation strings (may be `[]`).
  - `free_names`: identifiers the body references that aren't locally
    bound (feeds context packing; `[]` is acceptable v1).
  - `pins`: see **Pins** below.
- **`ClassDecl(node, name, methods, fields)`** — for any named type that
  owns methods or fields (structs, impl targets, interfaces).
  - `methods`: list of FunctionDecl (their params exclude the receiver).
  - `fields`: `dict[field_name, type_name]` — **powers DI resolution**: a
    call `self.client.fetch()` resolves through the declared type of
    `client`. For plain data shapes (structs without methods) fields also
    feed shape-drift detection; use the base type name for DI-relevant
    fields (strip pointers/references), full type text is fine for
    data-only shapes.
- **`ImportDecl(node, target, alias)`** — `target` is the imported module
  as a **dotted path** (`std::collections::HashMap` →
  `std.collections.HashMap`); `alias` is the local name it binds (the last
  segment when unaliased). Resolution against in-repo modules happens
  centrally by exact/unique-suffix match on dotted module names.
- **`VariableDecl(node, name)`** — module-level constants/statics.

### `call_sites(self, func_node, source) -> list[CallSite]`
`CallSite = tuple[str, list[str], int]` — (dotted callee text, argument
identifier names, 0-based line). Rules:
- A plain call `f(x)` → `("f", ["x"], line)`.
- A path/method call → dotted text: `store::save(k)` → `"store.save"`;
  `client.fetch(id)` → `"client.fetch"`.
- **Receiver normalization (required for DI):** inside a method, calls
  through the method's own receiver must be emitted with the literal
  receiver replaced by `self`: in `fn sync(&self)` a call
  `self.client.fetch(id)` → `"self.client.fetch"`. (In Go, where receivers
  are arbitrarily named, `s.client.Fetch` must become
  `"self.client.Fetch"`.) The central resolver keys on the `self.`/`this.`
  prefix + the owning class's `fields` map.
- Skip panic/raise intrinsics (they're effects, not calls).

### `direct_effects(self, func_node, source, aliases) -> set[str]`
Effect tags directly present in the body. Tags: `io`, `net`, `fs`, `db`,
`nondeterm`, `raise`. `aliases` maps local import names → absolute dotted
targets (`r` → `requests`), built centrally from your ImportDecls —
normalize dotted callees through it before matching your tables.
Implement as `return set(self.direct_effects_confidence(...))`.

### `direct_effects_confidence(self, func_node, source, aliases) -> dict[str, str]`
The real classifier: `{tag: "high" | "lexical"}`.
- `"high"`: exact or prefix matches against curated tables of known APIs
  (`reqwest.`, `std.fs.`, `println!`) — ideally alias-normalized.
- `"lexical"`: guesses from bare method suffixes or receiver names
  (`anything.now()`, `db.query(...)` gated only by the receiver being
  named `db`). These are *reported but don't fail builds by default* — be
  honest about which of your rules are guesses.
If a tag matches both ways, high wins. Raise/panic detection is `"high"`.

### `function_body(self, func_node) -> TSNode | None`
The body block node (usually `child_by_field_name("body")`).

### `block_statements(self, block) -> list[TSNode]`
The block's statement nodes, comments filtered
(`[c for c in block.named_children if c.type != "comment"]`).

### `describe_statement(self, node, source) -> StatementDesc`
Classify one statement into exactly one descriptor. The CFG builder wires
topology from these; **you never recurse into sub-blocks yourself** — you
hand back the sub-block *nodes* and the builder calls you again for their
statements.

- **`AssignDesc(writes, mutates, reads)`** — assignments/let-bindings.
  `writes`: plain names bound; `mutates`: base names of attribute/index
  targets (`self.x = v` → mutates `["self"]`, `xs[0] = v` → `["xs"]`);
  `reads`: identifiers read on the RHS.
- **`BranchDesc(reads, consequence, else_block, next_branch)`** — an
  if-arm. `reads`: condition identifiers. `consequence`: then-block node.
  For `else if`, set `next_branch` to the nested if node (it will be
  described again); for a plain `else`, set `else_block` to its block.
- **`LoopDesc(reads, writes, body)`** — all loop forms. `writes`: loop
  variables (`for x in xs` → `["x"]`).
- **`ReturnDesc(reads, mutates)`** — returns; `reads` from the returned
  expression.
- **`MatchDesc(cases=[CaseDesc(node, reads, consequence)])`** — match/
  switch. `reads`: the scrutinee identifiers (repeat per case is fine);
  `consequence`: the case's body node (the case node itself is acceptable
  when the grammar nests statements directly under it).
- **`TryDesc(body, handlers=[HandlerDesc(node, writes, block)], else_block,
  finally_block)`** — try/catch shapes; skip if the language has none.
- **`WithDesc(writes, reads, body)`** — resource-acquisition headers;
  skip if none.
- **`SimpleDesc(reads, mutates)`** — everything else (expression
  statements). Put mutator-method receivers in `mutates` if you track
  them; `reads` from the expression.

Purity note: a function is classified `pure_function` only if it has no
impure effects **and** no caller-observable mutation — `mutates` on a
parameter or module global is what flips it to `state_transformer`.

### Optional: `classify_calls(self, node, source, aliases) -> dict[str, str]`
Same classification as `direct_effects_confidence` but over an *arbitrary*
subtree — powers statement-level effect location for `cgir decompose`.
Implement it as the real walker and have `direct_effects_confidence`
delegate (body lookup + `classify_calls`). Default: empty — decompose
reports your language as unsupported rather than guessing.

### Optional: `global_declared_names(self, func_node, source) -> set[str]`
Names the function declares as outer-scope (Python `global`/`nonlocal`).
Assignments to them count as mutations. Default: empty set — correct for
most languages.

## Pins (required wiring in `module_declarations`)

Pins are `cgir:` comment pragmas (`// cgir: pure`). `PinIndex` does the
extraction; you wire it:

```python
def module_declarations(self, root, source, module_name, rel_path):
    pin_index = PinIndex(root, source)   # works on any grammar with "comment" nodes
    ...
    # per definition — pass the OUTERMOST node (so a pin above an
    # attribute/export/pub keyword is found):
    fn = FunctionDecl(..., pins=pin_index.for_definition(outermost_node))
    ...
    # module-level pins: a header comment block applies to every function
    # in the file — UNLESS it sits directly above a pinnable definition
    # (then it's that definition's pin). Pass the first decl's row only
    # when that first decl is a function/class-like node; imports and
    # plain statements must pass None-equivalent:
    first = next((c for c in root.named_children if c.type != "comment"), None)
    pinnable = {"function_item", "struct_item", "impl_item"}  # your grammar's types
    module_pins = pin_index.module_pins(
        first.start_point[0] if first is not None and first.type in pinnable else None
    )
    if module_pins:
        for decl in decls:
            if isinstance(decl, FunctionDecl):
                decl.pins = sorted(set(decl.pins) | set(module_pins))
            elif isinstance(decl, ClassDecl):
                for m in decl.methods:
                    m.pins = sorted(set(m.pins) | set(module_pins))
    return decls
```

## Registration

**In-tree:** add your adapter to the tuple in
`src/cgir/languages/registry.py` (`_BUILTINS`).

**As a plugin package** (no fork):

```toml
[project.entry-points."cgir.languages"]
rust = "cgir_rust:RustAdapter"
```

Builtins win extension conflicts; broken plugins degrade to warnings.
`cgir languages` shows what loaded.

## The test bar

Mirror this pipeline-level suite (this is the acceptance standard; adapt
the code snippets to your language):

```python
from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.component_spec import ComponentKind
from cgir.ir.edges import EdgeKind
from cgir.ir.nodes import NodeKind
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource

def _scan(tmp_path):
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    build_cfg(graph, tmp_path)
    effects = classify(graph, tmp_path)
    purity = score(graph, effects)
    return {s.id: s for s in slice_components(graph, effects=effects, purity_scores=purity)}
```

Cover at least:
1. functions and methods ingested with correct ids
   (`<module>.<fn>` / `<module>.<Type>.<method>`). Note the graph split:
   free functions are `NodeKind.Function` with `func:` node ids; methods
   are `NodeKind.Method` with `method:` ids — queries must use the right
   prefix.
2. params + signature extracted; `spec.language == "<your name>"`
3. effect detection per tag you implement, incl. a pure function staying
   `ComponentKind.pure_function` with `purity == 1.0`
4. struct/class `fields` extracted
5. receiver-field call resolves:
   `graph.out_edges("method:<mod>.<Type>.<m>", EdgeKind.CALLS)` contains
   the target method (this proves your `self.` normalization)
6. caller of an effectful callee gets `"calls_effectful"` in effects
7. CFG: a function with if + loop has `NodeKind.Branch` and
   `NodeKind.Loop` children
8. a `// cgir: pure` pin lands in `spec.pins`
9. cross-file call resolution through your ImportDecls

**Spec field names** (exact): parameters land in `spec.inputs`
(`list[str]` of names, not ParamDecl objects); kinds are
`pure_function | state_transformer | effect_adapter | orchestrator |
unknown` (there is no `ComponentKind.function`); graph edges from
`graph.out_edges(...)` are `Edge` objects — the target is `edge.dst`,
not a tuple index. CFG `MatchDesc` cases materialize as `NodeKind.Branch`
chains in the graph (there is no `NodeKind.Match`).

**Unregistered-adapter warning (applies to every pass, not just
ingest):** each analysis that reads source (`build_call_graph`,
`build_cfg`, `classify`) resolves the adapter per file extension over
*registered* adapters, and **silently produces empty results** for files
nobody claims. During development either register your adapter (in-tree
tuple or installed entry point) or pass it explicitly everywhere:

```python
adapter = RustAdapter()
graph = TreeSitterSource(adapter=adapter).ingest(tmp_path)
tables = build_symbol_tables(graph)
build_call_graph(graph, tables, tmp_path, adapter=adapter)
build_cfg(graph, tmp_path, adapter=adapter)
effects = classify(graph, tmp_path, adapter=adapter)
```

...and pass `language="<your name>"` to `slice_components` — the automatic
per-file language lookup only sees *registered* adapters. (Once your
adapter is registered — in-tree or via entry point — none of this section
applies; plain calls work.)

## Honesty requirements

Document your known limits in the module docstring (which dynamic features
you can't see; which effect rules are lexical guesses). CGIR's credibility
is that its claims are checkable — an adapter that silently over-claims
purity is worse than none.
