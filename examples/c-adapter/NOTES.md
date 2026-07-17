# NOTES.md — C adapter implementation findings

## Summary

72 tests pass, all 9 bar points covered. Adapter file: `c_adapter.py`.
Test file: `test_c_adapter.py`.

---

## Doc gaps found

### 1. `slice_components` language parameter undocumented

`writing-an-adapter.md` says "pass it explicitly everywhere" for unregistered
adapters and lists `TreeSitterSource`, `build_call_graph`, `build_cfg`,
`classify` as the explicit-adapter functions. It does NOT mention
`slice_components`.

**Observed:** `slice_components` accepts a `language: str = "python"` parameter
that is used to set `spec.language` on all function/method components. If you
omit it, every spec gets `language='python'` regardless of the adapter used,
because `_node_language()` inside `slice_components` calls
`adapter_for_extension(node.path.suffix)` against the *registered* adapters —
unregistered adapters are invisible to this lookup.

**Fix needed in doc:** add `language="<your_name>"` to the `slice_components`
call in the test-bar example:

```python
specs = {s.id: s for s in slice_components(
    graph, effects=effects, purity_scores=purity, language="c"
)}
```

### 2. `spec.params` vs `spec.inputs`

The doc's test-bar text says "params + signature extracted" and the example
uses `spec.params` in prose, but `ComponentSpec` does not have a `.params`
field. The correct field is `.inputs` (a `list[str]` of param names, not
`list[ParamDecl]`). The doc should clarify this.

### 3. `ComponentKind.function` does not exist

The doc's test-bar says "functions and methods ingested with correct ids
(`<module>.<fn>` / `<module>.<Type>.<method>`)" and refers to
`NodeKind.Function` and `NodeKind.Method`, which is accurate. However, it is
easy to assume `ComponentKind.function` exists by analogy — it does not. The
actual `ComponentKind` values are:

- `pure_function` — no effects, no mutations
- `state_transformer` — no effects but mutates state
- `effect_adapter` — has direct effects
- `orchestrator` — calls effectful callees
- `unknown` — placeholder score

The doc should list these or note they are determined by CGIR's classifier,
not by the adapter.

### 4. `NodeKind.Match` does not exist

The doc mentions `MatchDesc(cases=...)` for switch/match, and the test-bar
example queries `NodeKind.Match`. But the actual `NodeKind` enum has no
`Match` member. Switch statements that produce a `MatchDesc` seem to be
handled differently in the CFG builder — the resulting graph nodes are
`NodeKind.Branch` or `NodeKind.Statement` depending on the CGIR version
installed. Tests should not check for `NodeKind.Match`.

### 5. `out_edges` returns `Edge` objects, not tuples

The doc example `graph.out_edges("method:...", EdgeKind.CALLS)` implies you
iterate and check for the target. The return type is `list[Edge]` where each
`Edge` has `.src`, `.dst`, `.kind`, `.attrs`. Tests checking "contains the
target method" should use `e.dst` not `e[1]`.

### 6. Spec `id` is the dotted qualname, not the graph node id

`slice_components` returns specs with `id = qualname` (e.g. `"math.add"`),
not the graph node id (e.g. `"func:math.add"`). The doc's test-bar text says
"queries must use the right prefix" when referring to graph nodes, but the
`_scan()` helper that returns `{s.id: s}` is keyed by qualname. This is
confusing when you try to look up `spec` by `func:` prefix — it won't be
there.

### 7. PinIndex comment node types for C

`writing-an-adapter.md` says "grammars disagree — `line_comment`/`block_comment`
in rust/c/java". However `COMMENT_NODE_TYPES` (the set used by PinIndex) is
`frozenset({'block_comment', 'line_comment', 'comment', 'doc_comment'})`. The
C grammar uses `comment` (not `line_comment`/`block_comment`) for both `//`
and `/* */` comment nodes. PinIndex works correctly because `comment` is in
the set, but the doc's advice to filter with your grammar's comment types is
accurate for `block_statements`. For C, use `"comment"` as the type to filter.

---

## C-specific limits (honesty requirements)

### Cross-file bare-name call resolution does not work

The doc mentions "unique-suffix fallback" for ImportDecl target resolution,
but this fallback lives in `_resolve_target()` (called only by
`build_symbol_tables` when processing `ImportDecl` nodes). Call-site
resolution in `_resolve_callee()` (called during `build_call_graph`) only
looks in the calling module's local symbol-table bindings.

**Consequence:** a C file calling `compute(val)` where `compute` is defined in
another `.c` file will **not** produce a CALLS edge, even if the files are in
the same repo and `compute` is uniquely named.

**What does work:**
- Same-module calls always resolve (both functions in the same `.c` file).
- `#include "utils.h"` emits `ImportDecl(target="utils", alias="utils")`,
  which binds `utils → module:utils` in the caller's symbol table.
- A hypothetical call to `utils.something` *would* resolve through the module
  table binding, but C syntax doesn't use dotted module paths.

**Workaround (not implemented):** the adapter could parse each `#include`'d
`.h` file at `module_declarations` time, extract function prototypes, and emit
`ImportDecl(target="utils.fn_name", alias="fn_name")` for each. This would
cause `build_symbol_tables` to bind `fn_name → func:utils.fn_name` via the
unique-suffix fallback in `_resolve_target`, making cross-file calls resolve.
This was not implemented because: (a) the task said to document, not force;
(b) parsing headers at ingest time adds complexity; (c) ambiguous function
names across headers would silently misresolve.

### Global mutation is not tracked

C has no `global` or `nonlocal` keyword. `global_declared_names` returns the
empty set (the correct default). Assignments like `global_counter++` inside a
function body are indistinguishable from local variable writes without a
full pre-pass to identify which names are module-level globals. The adapter
does not perform this pre-pass. Functions that only mutate module globals will
be mis-classified as `pure_function` when they should be `state_transformer`.

### Function pointers

Function pointer declarations (`void (*fn)(int)`) and calls through function
pointers (`(*fn)(x)` or `fn(x)`) are not distinguished from plain identifier
calls. The call site will be extracted with callee text `"fn"` which may or
may not resolve. Dynamic dispatch through function pointers is not tracked.

### typedef struct alias (no fields)

`typedef struct Foo Bar` (aliasing a named struct without a body) produces no
`ClassDecl`. Only `typedef struct { ... } Name` and `struct Name { ... }` with
an inline body produce a `ClassDecl`. This is documented behavior.

### Deeply nested pointer declarators

The adapter handles up to two levels of nesting in pointer/array declarators
(e.g. `int **name`, `int (*fn)()`, `int *(*make)()`). Exotic C declarators
with more nesting may fail to extract the function name and will be silently
skipped (returns `None` from `_extract_function_name`).

### `assert` classified as `raise`

C's `assert(cond)` appears as a `call_expression` with callee `assert`. The
adapter classifies it as `raise: "high"` since assertion failure terminates
the process — analogous to `abort()`. In practice, `assert` is often disabled
in release builds (`-DNDEBUG`), so callers may not actually be at risk. This
is conservative-side over-claiming; document it per the honesty requirement.

### Effect aliases

`direct_effects_confidence` receives an `aliases` dict but most C programs do
not use dotted-import aliasing (unlike Python/Rust). The alias normalization
works correctly (normalizes the callee head through the dict) but in practice
will rarely fire for C codebases.

---

## What worked smoothly

- tree-sitter-c grammar at 0.23.2 is stable and well-structured.
- `function_declarator` / `pointer_declarator` nesting is regular enough to
  handle with a recursive finder.
- struct field extraction is clean (field_declaration_list → field_declaration).
- CFG branch, loop, switch, return, and assignment all map directly to
  CGIR's descriptor types.
- PinIndex works with C comment nodes (`comment` type covers both `//` and
  `/* */` in the C grammar).
- Effects tables for C stdlib/POSIX/SQLite3 are straightforward.
- `lexical` vs `high` confidence is honest: receiver-name gating (`db->query`)
  is correctly reported as `"lexical"`, not `"high"`.
