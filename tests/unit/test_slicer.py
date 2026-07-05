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


def test_entrypoint_detected_on_route(tmp_path: Path) -> None:
    """Sprint 17: decorated routes carry their entrypoint label on the spec."""
    specs = _specs_for(
        tmp_path,
        """
        @router.get("/novels/{novel_id}")
        def get_novel(novel_id: int):
            return novel_id
        """,
    )
    assert specs["m.get_novel"].entrypoint == "HTTP GET /novels/{novel_id}"


def test_plain_function_has_no_entrypoint(tmp_path: Path) -> None:
    specs = _specs_for(tmp_path, "def f(x):\n    return x\n")
    assert specs["m.f"].entrypoint is None


def test_raise_only_route_is_not_effect_adapter(tmp_path: Path) -> None:
    """Raising HTTPException alone must not lump a route in with real I/O."""
    specs = _specs_for(
        tmp_path,
        """
        def get_item(i):
            if i < 0:
                raise ValueError("bad")
            return i
        """,
    )
    assert specs["m.get_item"].kind == ComponentKind.pure_function
    assert "raise" in specs["m.get_item"].effects


def test_db_function_is_effect_adapter(tmp_path: Path) -> None:
    specs = _specs_for(tmp_path, "def find(db, i):\n    return db.query(i).first()\n")
    assert specs["m.find"].kind == ComponentKind.effect_adapter
    assert "db" in specs["m.find"].effects


def test_constructor_with_init_resolves_to_init(tmp_path: Path) -> None:
    """`C(...)` where C defines __init__ lands in calls as m.C.__init__."""
    specs = _specs_for(
        tmp_path,
        """
        class C:
            def __init__(self, x):
                self.x = x

        def make(x):
            return C(x)
        """,
    )
    assert "m.C.__init__" in specs["m.make"].calls
    assert specs["m.make"].constructs == []


def test_constructor_without_init_lands_in_constructs(tmp_path: Path) -> None:
    """`C(...)` with no __init__ (dataclass/ORM style) records constructs."""
    specs = _specs_for(
        tmp_path,
        """
        class C:
            pass

        def make():
            return C()
        """,
    )
    assert specs["m.make"].constructs == ["m.C"]
    assert "m.C" not in specs["m.make"].calls


def test_outputs_from_return_annotation(tmp_path: Path) -> None:
    specs = _specs_for(tmp_path, "def add(a: int, b: int) -> float:\n    return a + b\n")
    assert specs["m.add"].outputs == ["float"]


def test_outputs_empty_without_annotation(tmp_path: Path) -> None:
    specs = _specs_for(tmp_path, "def add(a, b):\n    return a + b\n")
    assert specs["m.add"].outputs == []


def test_db_session_delete_is_effect_adapter(tmp_path: Path) -> None:
    """`db.delete(ch)` is DB access — effect_adapter with the db tag (Sprint 13).

    Originally found misclassified as pure on a real repo; briefly pinned as
    state_transformer before the settled taxonomy made DB access an effect.
    """
    specs = _specs_for(tmp_path, "def remove(db, ch):\n    db.delete(ch)\n")
    assert specs["m.remove"].kind == ComponentKind.effect_adapter
    assert "db" in specs["m.remove"].effects


def test_delete_on_non_db_receiver_is_state_transformer(tmp_path: Path) -> None:
    """The `delete`/`remove` mutator entries still fire without a DB receiver."""
    specs = _specs_for(tmp_path, "def drop(registry, key):\n    registry.remove(key)\n")
    assert specs["m.drop"].kind == ComponentKind.state_transformer


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
