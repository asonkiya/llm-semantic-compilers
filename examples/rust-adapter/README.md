# Rust adapter — the docs-only experiment

Rust support was written by an **independent agent (Claude Sonnet) given
only [`docs/writing-an-adapter.md`](../../docs/writing-an-adapter.md)** —
no access to CGIR's source — as a usability test of the plugin docs. It hit
all 9 points of the doc's test bar (36/36 tests) in one sitting; the doc
gaps it surfaced were folded back into the guide, and its biggest find —
`PinIndex` missing `line_comment` grammars — became a core fix.

The adapter was then reviewed and **promoted to a builtin**:
`src/cgir/languages/rust.py` (tests: `tests/unit/test_rust_adapter.py`).
`NOTES.md` here preserves the agent's original gap report and limits list.

The takeaway for plugin authors: the guide is sufficient — an implementer
who has never seen the codebase produced a promotable adapter from it.
