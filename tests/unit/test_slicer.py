from pathlib import Path
from textwrap import dedent

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource


def _specs_for(repo: Path, code: str) -> dict[str, ComponentSpec]:
    (repo / "m.py").write_text(dedent(code).lstrip())
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    build_cfg(graph, repo)
    effects = classify(graph, repo)
    purity = score(graph, effects)
    return {s.id: s for s in slice_components(graph, effects=effects, purity_scores=purity)}


def test_pricing_add_tax_spec(python_sample_repo: Path) -> None:
    graph = TreeSitterSource().ingest(python_sample_repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, python_sample_repo)
    build_cfg(graph, python_sample_repo)
    effects = classify(graph, python_sample_repo)
    purity = score(graph, effects)

    specs = {
        spec.id: spec for spec in slice_components(graph, effects=effects, purity_scores=purity)
    }
    add_tax = specs["pricing.add_tax"]

    assert add_tax.inputs == ["price", "rate"]
    assert add_tax.calls == []
    assert add_tax.trace == ["pricing.py:1"]
    assert add_tax.language == "python"
    assert add_tax.kind == ComponentKind.pure_function
    assert add_tax.purity == 1.0
    add_tax.validate()

    quote = specs["orchestrator.quote"]
    assert "pricing.add_tax" in quote.calls
    assert quote.kind == ComponentKind.pure_function


def test_method_mutating_self_is_state_transformer(tmp_path: Path) -> None:
    """A method whose body mutates ``self.<attr>`` classifies as state_transformer.

    Drives the slicer to use the new ``Assignment.attrs["mutates"]`` populated
    by the CFG pass — distinguishing a state-mutating method from a true
    ``pure_function``.
    """
    repo = tmp_path
    (repo / "m.py").write_text(
        dedent(
            """
            class C:
                def set_x(self, v):
                    self.x = v
            """
        ).lstrip()
    )

    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    build_cfg(graph, repo)
    effects = classify(graph, repo)
    purity = score(graph, effects)

    specs = {
        spec.id: spec for spec in slice_components(graph, effects=effects, purity_scores=purity)
    }
    assert specs["m.C.set_x"].kind == ComponentKind.state_transformer


def test_mutator_method_call_is_state_transformer(tmp_path: Path) -> None:
    """`xs.append(x)` mutates its argument — not a pure_function (Sprint 5)."""
    specs = _specs_for(tmp_path, "def push(xs, x):\n    xs.append(x)\n")
    assert specs["m.push"].kind == ComponentKind.state_transformer


def test_augmented_self_assignment_is_state_transformer(tmp_path: Path) -> None:
    """`self.total += n` mutates self — state_transformer (Sprint 5)."""
    specs = _specs_for(
        tmp_path,
        """
        class C:
            def bump(self, n):
                self.total += n
        """,
    )
    assert specs["m.C.bump"].kind == ComponentKind.state_transformer


def test_mutating_a_local_is_not_state_transformation(tmp_path: Path) -> None:
    """Appending to a list the function itself created is invisible to callers."""
    specs = _specs_for(
        tmp_path,
        """
        def collect(items):
            out = []
            for i in items:
                out.append(i)
            return out
        """,
    )
    assert specs["m.collect"].kind == ComponentKind.pure_function


def test_mutating_a_global_is_state_transformation(tmp_path: Path) -> None:
    specs = _specs_for(
        tmp_path,
        """
        CACHE = {}

        def remember(k, v):
            CACHE.update({k: v})
        """,
    )
    assert specs["m.remember"].kind == ComponentKind.state_transformer


def test_rhs_pop_is_state_transformer(tmp_path: Path) -> None:
    """`return xs.pop()` mutates the argument — not a pure_function (Sprint 11)."""
    specs = _specs_for(tmp_path, "def take(xs):\n    return xs.pop()\n")
    assert specs["m.take"].kind == ComponentKind.state_transformer


def test_subscript_write_to_local_is_not_state_transformation(tmp_path: Path) -> None:
    specs = _specs_for(
        tmp_path,
        """
        def index(items):
            table = {}
            for i in items:
                table[i] = True
            return table
        """,
    )
    assert specs["m.index"].kind == ComponentKind.pure_function
