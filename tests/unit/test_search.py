"""Ranked, structured component search — the agent front door.

Free terms rank by where they hit (name > id > entrypoint > doc); colon
predicates filter on the contract (kind/effects/lexical/callers/pins/...) —
queries no vector index can answer.
"""

from __future__ import annotations

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.search import search_specs


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    effects: list[str] | None = None,
    lexical: list[str] | None = None,
    calls: list[str] | None = None,
    pins: list[str] | None = None,
    entrypoint: str | None = None,
    doc: str | None = None,
    language: str = "python",
    covered_by: list[str] | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        effects=effects or [],
        lexical_effects=lexical or [],
        calls=calls or [],
        pins=pins or [],
        entrypoint=entrypoint,
        doc=doc,
        language=language,
        covered_by=covered_by or [],
        trace=[f"{spec_id}.py:1"],
    )


CORPUS = [
    _spec("app.pricing.add_tax", doc="Multiply price by 1+rate."),
    _spec(
        "app.api.charge",
        kind=ComponentKind.effect_adapter,
        effects=["net", "db"],
        lexical=["db"],
        calls=["app.pricing.add_tax"],
        entrypoint="HTTP POST /charge",
    ),
    _spec(
        "app.api.list_taxes",
        kind=ComponentKind.effect_adapter,
        effects=["db"],
        lexical=["db"],
        calls=["app.pricing.add_tax"],
        entrypoint="HTTP GET /taxes",
    ),
    _spec("app.util.tax_label", calls=["app.pricing.add_tax"], pins=["pure"]),
    _spec(
        "svc.store.SaveKey",
        kind=ComponentKind.effect_adapter,
        effects=["io"],
        language="go",
        covered_by=["tests.test_store.test_save"],
    ),
]


def _ids(query: str) -> list[str]:
    return [s.id for s in search_specs(CORPUS, query)]


def test_exact_name_ranks_first() -> None:
    ids = _ids("add_tax")
    assert ids[0] == "app.pricing.add_tax"


def test_doc_hit_ranks_below_name_hit() -> None:
    # "price" hits add_tax's doc only; "charge" hits a name.
    assert _ids("price") == ["app.pricing.add_tax"]


def test_kind_predicate_with_alias() -> None:
    assert set(_ids("kind:pure")) == {"app.pricing.add_tax", "app.util.tax_label"}
    assert set(_ids("kind:effect_adapter")) == {
        "app.api.charge",
        "app.api.list_taxes",
        "svc.store.SaveKey",
    }


def test_effects_predicate() -> None:
    assert _ids("effects:net") == ["app.api.charge"]
    assert set(_ids("effects:none")) == {"app.pricing.add_tax", "app.util.tax_label"}


def test_lexical_predicate() -> None:
    # lexical:false = every effect is table-verified
    assert "app.api.charge" not in _ids("effects:db lexical:false")
    assert set(_ids("effects:db lexical:true")) == {"app.api.charge", "app.api.list_taxes"}


def test_callers_predicate() -> None:
    # add_tax has 3 callers in the corpus
    assert _ids("callers:>2") == ["app.pricing.add_tax"]
    assert "svc.store.SaveKey" in _ids("callers:0")


def test_pins_and_entrypoint_predicates() -> None:
    assert _ids("pinned:true") == ["app.util.tax_label"]
    assert _ids("pins:pure") == ["app.util.tax_label"]
    assert set(_ids("entrypoint:true")) == {"app.api.charge", "app.api.list_taxes"}
    assert _ids("entrypoint:POST") == ["app.api.charge"]


def test_lang_and_covered_predicates() -> None:
    assert _ids("lang:go") == ["svc.store.SaveKey"]
    # effectful but untested: charge + list_taxes (SaveKey is covered)
    assert set(_ids("kind:effect_adapter covered:false")) == {
        "app.api.charge",
        "app.api.list_taxes",
    }


def test_predicates_combine_with_terms() -> None:
    assert _ids("effects:db tax") == ["app.api.list_taxes"]


def test_unknown_predicate_treated_as_term() -> None:
    # not a registered predicate — falls back to a free term, no crash
    assert search_specs(CORPUS, "weird:thing") == []


def test_empty_query_returns_nothing() -> None:
    assert search_specs(CORPUS, "   ") == []
