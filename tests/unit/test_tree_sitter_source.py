from pathlib import Path
from textwrap import dedent

from cgir.ir.nodes import NodeKind
from cgir.sources import TreeSitterSource


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(body).lstrip())


def test_ingests_fixture(python_sample_repo: Path) -> None:
    graph = TreeSitterSource().ingest(python_sample_repo)

    files = {n.name for n in graph.nodes(NodeKind.File)}
    assert files == {"pricing.py", "orchestrator.py"}

    funcs = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert {"pricing.add_tax", "orchestrator.quote"} <= funcs

    params = {n.name for n in graph.nodes(NodeKind.Parameter)}
    assert {"price", "rate"} <= params


# --- Ignore dirs ------------------------------------------------------------


def test_default_ignore_skips_venv(tmp_path: Path) -> None:
    _write(tmp_path, "real.py", "def keep():\n    pass\n")
    _write(tmp_path, "venv/lib/python3.11/site-packages/junk.py", "def skip():\n    pass\n")
    graph = TreeSitterSource().ingest(tmp_path)
    qualnames = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert "real.keep" in qualnames
    assert not any("skip" in (q or "") for q in qualnames)


def test_default_ignore_skips_common_output_dirs(tmp_path: Path) -> None:
    for d in ["node_modules", "build", "dist", "__pycache__", "site-packages"]:
        _write(tmp_path, f"{d}/junk.py", "def skip():\n    pass\n")
    _write(tmp_path, "keep.py", "def keep():\n    pass\n")
    graph = TreeSitterSource().ingest(tmp_path)
    qualnames = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert "keep.keep" in qualnames
    assert not any("skip" in (q or "") for q in qualnames)


def test_dot_prefixed_dirs_still_skipped(tmp_path: Path) -> None:
    """``.tox`` / ``.git`` / ``.venv`` etc. — existing dot-prefix behaviour."""
    _write(tmp_path, ".tox/py311/junk.py", "def skip():\n    pass\n")
    _write(tmp_path, "keep.py", "def keep():\n    pass\n")
    graph = TreeSitterSource().ingest(tmp_path)
    qualnames = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert "keep.keep" in qualnames
    assert not any("skip" in (q or "") for q in qualnames)


def test_custom_ignore_dirs_extends_default(tmp_path: Path) -> None:
    _write(tmp_path, "vendor/lib.py", "def skip():\n    pass\n")
    _write(tmp_path, "keep.py", "def keep():\n    pass\n")
    graph = TreeSitterSource(ignore_dirs={"vendor"}).ingest(tmp_path)
    qualnames = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert "keep.keep" in qualnames
    assert not any("skip" in (q or "") for q in qualnames)


def test_custom_ignore_does_not_replace_default(tmp_path: Path) -> None:
    """Passing custom ``ignore_dirs`` must extend, not override, the defaults."""
    _write(tmp_path, "vendor/lib.py", "def skip_vendor():\n    pass\n")
    _write(tmp_path, "node_modules/lib.py", "def skip_node():\n    pass\n")
    _write(tmp_path, "keep.py", "def keep():\n    pass\n")
    graph = TreeSitterSource(ignore_dirs={"vendor"}).ingest(tmp_path)
    qualnames = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert "keep.keep" in qualnames
    assert not any("skip" in (q or "") for q in qualnames)


# --- Decorated function / class definitions --------------------------------


def test_property_method_surfaced(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "m.py",
        """
        class C:
            @property
            def x(self):
                return self._x
        """,
    )
    graph = TreeSitterSource().ingest(tmp_path)
    methods = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Method)}
    assert "m.C.x" in methods


def test_staticmethod_classmethod_surfaced(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "m.py",
        """
        class C:
            @staticmethod
            def s():
                return 1

            @classmethod
            def c(cls):
                return 2
        """,
    )
    graph = TreeSitterSource().ingest(tmp_path)
    methods = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Method)}
    assert "m.C.s" in methods
    assert "m.C.c" in methods


def test_multi_decorator_stack_surfaced(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "m.py",
        """
        from functools import wraps

        def a(f): return f
        def b(f): return f

        @a
        @b
        def stacked(x):
            return x
        """,
    )
    graph = TreeSitterSource().ingest(tmp_path)
    funcs = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert "m.stacked" in funcs


def test_decorated_class_surfaced_with_methods(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "m.py",
        """
        def register(cls): return cls

        @register
        class Service:
            def handle(self):
                return 1
        """,
    )
    graph = TreeSitterSource().ingest(tmp_path)
    classes = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Class)}
    methods = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Method)}
    assert "m.Service" in classes
    assert "m.Service.handle" in methods
