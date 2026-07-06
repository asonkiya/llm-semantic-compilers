"""Contract tests for the LanguageAdapter seam (Phase 1).

Pin the adapter's grammar-specific extraction directly, so a new language's
adapter can be validated against the same shape.
"""

from __future__ import annotations

from cgir.languages import DEFAULT_ADAPTER, PythonAdapter, adapter_for_extension


def _fn(adapter: PythonAdapter, src: str):
    root = adapter.parse(src.encode())
    return root, src.encode()


def test_registry_resolves_python_by_extension() -> None:
    assert adapter_for_extension(".py") is not None
    assert adapter_for_extension(".py").name == "python"
    assert adapter_for_extension(".zzz") is None


def test_default_adapter_is_python() -> None:
    assert DEFAULT_ADAPTER.name == "python"


def test_locate_function_finds_by_name_and_row() -> None:
    a = PythonAdapter()
    root, src = _fn(a, "def a():\n    pass\n\ndef b(x):\n    return x\n")
    node = a.locate_function(root, "b", 3)
    assert node is not None
    assert node.child_by_field_name("name").text.decode() == "b"


def test_direct_effects_io_and_raise() -> None:
    a = PythonAdapter()
    root, src = _fn(a, "def f(x):\n    print(x)\n    raise ValueError('e')\n")
    fn = a.locate_function(root, "f", 0)
    assert a.direct_effects(fn, src, {}) == {"io", "raise"}


def test_direct_effects_alias_aware_net() -> None:
    a = PythonAdapter()
    root, src = _fn(a, "def f(u):\n    return r.get(u)\n")
    fn = a.locate_function(root, "f", 0)
    assert "net" in a.direct_effects(fn, src, {"r": "requests"})


def test_direct_effects_db_receiver() -> None:
    a = PythonAdapter()
    root, src = _fn(a, "def f(db, i):\n    return db.query(i)\n")
    fn = a.locate_function(root, "f", 0)
    assert "db" in a.direct_effects(fn, src, {})


def test_call_sites_dotted_and_args() -> None:
    a = PythonAdapter()
    root, src = _fn(a, "def f(x, y):\n    obj.method(x, y)\n")
    fn = a.locate_function(root, "f", 0)
    [(callee, args, line)] = a.call_sites(fn, src)
    assert callee == "obj.method"
    assert set(args) == {"x", "y"}  # obj is the receiver, not an argument
    assert line == 2
