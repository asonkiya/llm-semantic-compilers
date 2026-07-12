# Rust Adapter — Implementation Notes

## Doc gaps and ambiguities found during implementation

### 1. PinIndex comment type mismatch
**Gap:** The spec says "Wire pins using PinIndex per the doc's pattern," and PinIndex is documented as
"grammar-agnostic: both supported grammars use `comment` nodes." However, the Rust tree-sitter
grammar uses `line_comment` and `block_comment` node types, NOT `comment`. The base PinIndex
would silently miss all Rust `cgir:` pragmas.

**Resolution:** Implemented `RustPinIndex` — a complete reimplementation of PinIndex that extends
the type check to `{"comment", "line_comment", "block_comment"}`. This is a drop-in replacement
and the adapter uses it everywhere instead of PinIndex.

### 2. Pipeline adapter threading
**Gap:** The spec/doc does not mention that `classify`, `build_call_graph`, and `build_cfg` each
accept an optional `adapter` parameter. Without passing the RustAdapter, these functions call
`adapter_for_extension(".rs")` which returns `None` (since RustAdapter is a plugin not in the
builtin registry), silently giving empty results for effects, call sites, and CFG nodes.

**Resolution:** All pipeline calls must pass `adapter=adapter`. This is a required pattern for any
language plugin that is not registered in `cgir.languages.registry`. The test suite documents this
by always threading the adapter through.

### 3. `impl_item` type field name
**Gap:** The spec says "check if `impl_item.child_by_field_name('type')` works or if you need to
iterate children." `child_by_field_name("type")` works correctly — confirmed via grammar probing.
The field is indeed named `"type"` in the tree-sitter-rust 0.23.2 grammar.

### 4. `for_expression` field names
**Gap:** Spec was partially uncertain about `for`-loop field names. Confirmed via probing:
- `pattern` = loop variable (identifier)
- `value` = iterable expression
- `body` = the block

### 5. `if_expression` alternative field
**Gap:** Spec mentioned field might be `"alternative"`. Confirmed it IS `"alternative"` and
its child is `else_clause`. The `else_clause` contains either a `block` (for plain `else`) or
an `if_expression` (for `else if`).

### 6. `match_expression` fields
**Gap:** The spec mentioned field names might vary. Confirmed:
- `value` = the scrutinee expression
- `body` = match_block containing match_arm nodes
- Each `match_arm` has: `pattern` (match_pattern), `value` (the RHS expression/block)

### 7. Doc comment node naming
**Gap:** Spec says look for `line_comment` nodes with `outer_doc_comment_marker` child. This is
correct. The doc text is in a `doc_comment` named child within `line_comment`. However, doc
comment collection needs sibling-based walking (look backwards from function index in parent's
children list), not the tree-sitter grammar's parent/child relationship, since line_comment nodes
are siblings of function_item, not children.

### 8. `NodeKind.Function` vs `NodeKind.Method`
**Gap:** Not a Rust adapter issue per se, but test authors need to know: Rust impl methods are
ingested as `NodeKind.Method` (id prefix `method:`), while top-level functions are
`NodeKind.Function` (id prefix `func:`). `graph.nodes(NodeKind.Function)` returns only free
functions, not methods. Tests querying for methods must use `NodeKind.Method` or combine both.

### 9. `scoped_use_list` child structure
**Gap:** When parsing `use std::io::{Read, Write}`, the `scoped_use_list` node contains:
- An `identifier` or `scoped_identifier` child (the path prefix, e.g. `std::io`)
- A `use_list` named child containing `identifier` and `use_as_clause` items

The path prefix node is NOT always a `scoped_identifier` — for single-segment prefixes it's
`identifier`. The adapter handles both.

---

## Known limits

### Effect detection limits

1. **Macro expansion:** Macros beyond `println!`, `panic!`, etc. that expand to effectful calls
   are not detected. CGIR's design flags dynamic dispatch/macros as precision limits.

2. **Trait object calls:** `dyn Trait` method calls cannot be resolved to concrete implementations
   without type inference. These appear as call sites but won't resolve to specific methods.

3. **`async`/`.await`:** Async functions are parsed identically to sync ones. The `await` keyword
   appears as a child node in expressions but is not specially handled. Effects from async
   functions work correctly since the body is walked the same way.

4. **`?` operator (question mark / error propagation):** `expr?` desugars to an early return +
   `From::from` conversion. The `?` operator is not currently detected as a potential `raise`
   source (it's not a panic). This is intentional — `?` is value-based error handling, not an
   exception.

5. **`std::process::exit`:** Not in the effect tables. Could be added as a `raise`-like effect.

6. **`reqwest::blocking::` vs `reqwest::`:** Both map to `net` via the `reqwest.` prefix after
   `::` → `.` normalization.

### Call site resolution limits

7. **Chained method calls:** `c.get(url).send()` — the adapter resolves the outermost call
   (`send`) with a receiver that is itself a call expression. In this case `_field_expr_dotted`
   recursively resolves the chain, yielding the callee name. This is best-effort; very deeply
   chained expressions may produce incomplete dotted paths.

8. **Closure captures:** Calls inside closure bodies (`|| { foo() }`) are walked and collected,
   but they are attributed to the containing function, not to a separate closure component.

9. **`use crate::...` imports:** The `crate` prefix is left as-is (`crate.store.MyStore`). The
   CGIR central resolver must handle `crate` → current package name mapping. Without that mapping,
   cross-crate-relative imports won't resolve to their target functions.

10. **Conditional compilation (`#[cfg(...)]`):** Attribute filtering is not applied; all items
    are ingested regardless of `#[cfg]` attributes.

### CFG limits

11. **Implicit returns:** Rust blocks often return the last expression without `return`. These
    appear as bare expressions in the block's named_children (e.g., `identifier`, `binary_expression`)
    rather than `return_expression`. The adapter processes them as `SimpleDesc` nodes rather than
    `ReturnDesc`. CFG topology still terminates correctly since the block ends.

12. **`loop` + `break value`:** A `loop { ... break value; }` expression can return a value via
    `break`. The `break` is treated as a statement without connecting its value to a `ReturnDesc`.

13. **Pattern matching in `if let` / `while let`:** `if let Some(x) = expr` is parsed as
    `if_expression` with a `let_condition` in the condition field rather than a plain expression.
    The adapter extracts reads from the condition node generically, which collects identifiers
    from the pattern as reads rather than writes. This is a minor inaccuracy in the CFG.

### Structural limits

14. **`enum` items:** `enum` declarations are not ingested as classes or any other typed node.
    Enum variants are not extracted. This is a known omission — enums would need a new kind
    (or `ClassDecl`) with each variant as a pseudo-field.

15. **`trait` items:** Trait declarations are not ingested. Trait method signatures are invisible
    to the pipeline. Only concrete `impl` blocks are processed.

16. **Nested `impl` blocks / `impl Trait for Type`:** The adapter uses `child_by_field_name("type")`
    to get the implementing type. For `impl Display for MyStruct`, the field is `name` (the trait
    name) rather than `type` (the implementing type). The adapter handles the `"type"` field path
    and falls back to iterating named_children for a `type_identifier`. Trait impls where the
    implementing type is complex (generic bounds) may not resolve the struct name correctly.

17. **Same-crate cross-file calls without `use`:** Unlike Go (same-package = no import needed),
    Rust requires explicit `use` or full path qualification for cross-module calls. A bare call
    `helper(x)` in `b.rs` to `helper` defined in `a.rs` (same directory) will NOT resolve unless
    `b.rs` has `use crate::a::helper`. The `use crate::a::helper` import correctly creates an
    `ImportDecl` with `target="crate.a.helper"` and `alias="helper"`, which feeds the call graph.
