"""The rewrite-pair registry (:mod:`cgir.rewrite_pairs`) — plug-and-play for the
rewrite engine, mirroring the mature :mod:`cgir.languages` adapter registry.

Pins: the builtins are discoverable; a plugin adds a pair via an entry point;
the safety rules hold (broken plugin → warning not crash, builtin names win,
API-version mismatch loads with a warning); and a fresh SearchLoopPair runs a
worklist through the shared search loop end-to-end with a fake sampler (no
network), proving a new pair is one small class, not a re-implemented pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from cgir.rewrite_pairs import (
    PairContext,
    RewritePair,
    SearchLoopPair,
    available_pairs,
    discover_pairs,
    pair_for,
)


def test_builtins_are_registered():
    names = available_pairs()
    assert {"c-rust", "python-rust", "python-python"} <= set(names)
    cr = pair_for("c-rust")
    assert cr is not None and "differential" in cr.description.lower()
    assert isinstance(cr, RewritePair)  # runtime-checkable structural check


def test_unknown_pair_is_none():
    assert pair_for("cobol-haskell") is None


# --- a minimal third-party pair, as a plugin would ship it -----------------


@dataclass
class _FakeEP:
    name: str
    _obj: object

    def load(self):
        return self._obj


class _ReversePair(SearchLoopPair):
    """A trivial same-language pair: 'rewrite' = reverse the string; the oracle
    accepts iff reversing twice is identity. Enough to exercise the seam."""

    name = "reverse"
    description = "toy pair for tests"

    def worklist(self, index_dir, source, opts):
        return list(opts.get("items", [])), [("x", "excluded reason")]

    def build_prompt(self, entry, ctx):
        return f"reverse: {entry.payload}"

    def evaluate(self, entry, candidate, ctx):
        ctx.extra["seen"] = ctx.extra.get("seen", 0) + 1
        if candidate.strip() == entry.payload[::-1]:
            return "ok", "", {"verify": "roundtrip"}
        return "mismatch", "not the reverse", {}


@dataclass
class _Item:
    component_id: str
    payload: str


def test_plugin_pair_is_discovered_via_entry_point():
    pairs, notes = discover_pairs([_FakeEP("reverse", _ReversePair)])
    assert "reverse" in pairs and "c-rust" in pairs  # builtins retained
    assert notes == []


def test_broken_plugin_warns_never_crashes():
    class _Boom(_FakeEP):
        def load(self):
            raise RuntimeError("boom")

    pairs, notes = discover_pairs([_Boom("kaboom", None)])
    assert "kaboom" not in pairs and "c-rust" in pairs  # survived
    assert any("failed to load" in n for n in notes)


def test_non_pair_object_is_skipped():
    pairs, notes = discover_pairs([_FakeEP("bogus", object())])
    assert "bogus" not in pairs
    assert any("not a RewritePair" in n for n in notes)


def test_plugin_cannot_shadow_a_builtin():
    class _Impostor(SearchLoopPair):
        name = "c-rust"
        description = "malicious override"

    pairs, notes = discover_pairs([_FakeEP("c-rust", _Impostor)])
    assert pairs["c-rust"].description != "malicious override"  # builtin kept
    assert any("reuses builtin name" in n for n in notes)


def test_api_version_mismatch_loads_with_warning():
    class _OldPair(SearchLoopPair):
        name = "old"
        description = "declares a stale api version"
        api_version = 999

    pairs, notes = discover_pairs([_FakeEP("old", _OldPair)])
    assert "old" in pairs  # loaded anyway
    assert any("api version" in n for n in notes)


def test_search_loop_pair_runs_end_to_end_with_fake_sampler():
    """The payoff: a new pair implements three small methods and inherits the
    whole engine. A fake sampler returns the correct reversal on the 2nd try
    for one item and never for another — exercising escalation + the ledger."""
    items = [_Item("a", "abc"), _Item("b", "xy")]

    calls = {"n": 0}

    def fake_sampler(prompt: str, model: str):
        calls["n"] += 1
        # 'abc' -> return correct reverse; 'xy' -> always wrong
        if "abc" in prompt:
            return ("```\ncba\n```", 0.001)
        return ("```\nnope\n```", 0.001)

    report = _ReversePair().run(
        index_dir=None,
        source=__import__("pathlib").Path("src.py"),
        sampler=fake_sampler,
        opts={"items": items},
        k=2,
    )
    by_id = {o["component_id"]: o for o in report["outcomes"]}
    assert by_id["a"]["status"] == "solved" and by_id["a"].get("verify") == "roundtrip"
    assert by_id["b"]["status"] == "unsolved"
    assert report["totals"]["solved"] == 1
    assert report["excluded"] == [("x", "excluded reason")]
    assert report["pair"] == "reverse"  # report_meta is spread into the report
    assert isinstance(report["_ctx"], PairContext)
