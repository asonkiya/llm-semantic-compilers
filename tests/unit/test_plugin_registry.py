"""Language plugin registry — third-party adapters via entry points.

A package declaring ``[project.entry-points."cgir.languages"]`` gets its
adapter discovered at startup: ``pip install cgir-rust`` is the whole
integration. Discovery is injectable for tests; hostile/broken plugins
degrade to warnings, never crashes.
"""

from __future__ import annotations

from tree_sitter import Node as TSNode

from cgir.languages.base import ADAPTER_API_VERSION, LanguageAdapter
from cgir.languages.registry import discover_adapters


class _FakeAdapter(LanguageAdapter):
    name = "fake"
    file_extensions = (".fake",)
    api_version = ADAPTER_API_VERSION

    def parse(self, source: bytes) -> TSNode:  # pragma: no cover - never called
        raise NotImplementedError

    def locate_function(self, root, name, start_row):  # pragma: no cover
        return None

    def direct_effects(self, func_node, source, aliases):  # pragma: no cover
        return set()

    def call_sites(self, func_node, source):  # pragma: no cover
        return []

    def function_body(self, func_node):  # pragma: no cover
        return None

    def block_statements(self, block):  # pragma: no cover
        return []

    def describe_statement(self, node, source):  # pragma: no cover
        raise NotImplementedError

    def module_declarations(self, root, source, module_name, rel_path):  # pragma: no cover
        return []


class _FakeEntryPoint:
    def __init__(self, name: str, obj: object, dist_name: str = "cgir-fake") -> None:
        self.name = name
        self._obj = obj
        self.dist = type("D", (), {"name": dist_name})()

    def load(self) -> object:
        return self._obj


def test_plugin_adapter_discovered() -> None:
    adapters, warnings = discover_adapters([_FakeEntryPoint("fake", _FakeAdapter)])
    assert "fake" in adapters
    assert adapters["fake"].file_extensions == (".fake",)
    assert warnings == []


def test_builtin_wins_extension_conflict() -> None:
    class Hijacker(_FakeAdapter):
        name = "hijack"
        file_extensions = (".py",)  # tries to claim Python

    adapters, warnings = discover_adapters([_FakeEntryPoint("hijack", Hijacker)])
    assert "hijack" not in adapters
    assert any(".py" in w for w in warnings)


def test_broken_plugin_degrades_to_warning() -> None:
    class Exploder:
        def load(self):
            raise RuntimeError("boom")

        name = "broken"

    adapters, warnings = discover_adapters([Exploder()])
    assert "broken" not in adapters
    assert any("broken" in w for w in warnings)


def test_non_adapter_object_rejected() -> None:
    adapters, warnings = discover_adapters([_FakeEntryPoint("junk", object)])
    assert "junk" not in adapters
    assert any("junk" in w for w in warnings)


def test_api_version_mismatch_warns_but_loads() -> None:
    class OldPlugin(_FakeAdapter):
        name = "old"
        file_extensions = (".old",)
        api_version = ADAPTER_API_VERSION - 1

    adapters, warnings = discover_adapters([_FakeEntryPoint("old", OldPlugin)])
    assert "old" in adapters  # defaults on the ABC keep old plugins viable
    assert any("api version" in w.lower() for w in warnings)


def test_duplicate_language_name_rejected() -> None:
    class Impostor(_FakeAdapter):
        name = "python"  # collides with the builtin
        file_extensions = (".xyz",)

    adapters, warnings = discover_adapters([_FakeEntryPoint("python", Impostor)])
    # the builtin survives; the impostor is rejected with a warning
    assert type(adapters["python"]).__name__ == "PythonAdapter"
    assert any("python" in w for w in warnings)


def test_instance_entry_point_supported() -> None:
    # entry point may resolve to an instance instead of a class
    adapters, _ = discover_adapters([_FakeEntryPoint("fake", _FakeAdapter())])
    assert "fake" in adapters


def test_source_cache_locate_is_indexed(tmp_path):
    """SourceCache.locate builds a one-walk per-file function index instead
    of re-walking the whole tree per lookup — the O(functions x tree) hot
    path found scanning SQLite's 270k-line amalgamation (rung 1)."""
    from pathlib import Path

    from cgir.languages.cache import SourceCache

    (tmp_path / "m.py").write_text(
        "def a():\n    return 1\n\n\nclass C:\n    def m(self):\n        return 2\n"
    )
    cache = SourceCache(Path(tmp_path))
    node = cache.locate("m.py", "a", 0)
    assert node is not None and node.start_point[0] == 0
    method = cache.locate("m.py", "m", 5)
    assert method is not None and method.start_point[0] == 5
    assert cache.locate("m.py", "nope", 0) is None
