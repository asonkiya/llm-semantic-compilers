# Rust adapter — the docs-only experiment

This adapter was written by an **independent agent (Claude Sonnet) given
only [`docs/writing-an-adapter.md`](../../docs/writing-an-adapter.md)** —
no access to CGIR's source — as a usability test of the plugin docs.
Result: all 9 points of the doc's test bar, 36/36 tests passing.

It is a *reference example*, not a shipped builtin: review `NOTES.md` for
its documented limits (enums/traits not ingested, `#[cfg]` ignored,
implicit tail-expression returns are SimpleDesc). The experiment's doc-gap
findings were folded back into the authoring guide, and its biggest
discovery — `PinIndex` missing `line_comment` grammars — is fixed in core.

To use: package it per the guide's plugin section, or pass
`TreeSitterSource(adapter=RustAdapter())` (+ `adapter=` on each analysis).

Run its tests:
```bash
pip install "tree-sitter-rust>=0.23,<0.24"
pytest examples/rust-adapter/test_rust_adapter.py
```
