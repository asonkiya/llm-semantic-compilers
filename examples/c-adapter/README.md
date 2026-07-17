# C adapter — docs-only experiment, round two

Second run of the protocol that produced the Rust adapter: an independent
agent (Claude Sonnet) implemented C support from
[`docs/writing-an-adapter.md`](../../docs/writing-an-adapter.md) alone —
no CGIR source access. Result: 72/72 tests, all 9 bar points (receiver-DI
sensibly substituted — C has no methods — with a cross-file resolution
test), and 5 new doc gaps found and folded back into the guide.

Promoted builtin: `src/cgir/languages/c.py`
(tests: `tests/unit/test_c_adapter.py`). During promotion its biggest
reported limit — cross-file bare-name calls not resolving — was fixed in
core: C's linker has one global namespace, so uniquely-defined external
names now merge repo-wide (`symbols._merge_c_globals`; ambiguous names
stay unresolved rather than guessed).

`NOTES.md` preserves the agent's original gap report. This is rung 1 of
[`docs/vision-rewrite.md`](../../docs/vision-rewrite.md).
