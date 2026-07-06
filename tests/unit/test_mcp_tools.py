"""RED-phase tests for the MCP tool functions (Sprint 19).

Contract:

* ``cgir.api.mcp_server`` exposes plain functions over an index dir —
  ``tool_stats``, ``tool_component``, ``tool_flow``, ``tool_pack``,
  ``tool_search``, ``tool_entrypoints`` — each returning a string an
  agent can consume directly. They are the substance; the FastMCP wiring
  is a thin adapter behind a lazy ``mcp`` import (``cgir[mcp]`` extra),
  mirroring the anthropic pattern.
* ``create_server`` raises a RuntimeError naming ``cgir[mcp]`` when the
  optional dependency is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cgir.api.mcp_server import (
    tool_component,
    tool_entrypoints,
    tool_flow,
    tool_pack,
    tool_search,
    tool_stats,
    tool_verify,
)
from cgir.cli import app


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = Path(__file__).parent.parent / "fixtures" / "python_sample"
    out = tmp_path_factory.mktemp("mcp-idx")
    result = CliRunner().invoke(app, ["scan", str(repo), "--out", str(out)])
    assert result.exit_code == 0, result.output
    return out


def test_tool_stats(index: Path) -> None:
    text = tool_stats(index)
    assert "Components: 2" in text


def test_tool_component(index: Path) -> None:
    text = tool_component(index, "pricing.add_tax")
    assert '"id": "pricing.add_tax"' in text


def test_tool_component_unknown(index: Path) -> None:
    assert "unknown component" in tool_component(index, "nope.x").lower()


def test_tool_flow(index: Path) -> None:
    text = tool_flow(index, "pricing.add_tax", depth=2)
    assert "orchestrator.quote" in text


def test_tool_pack(index: Path) -> None:
    text = tool_pack(index, "orchestrator.quote", budget=4000)
    assert text.startswith("# orchestrator.quote")
    assert "pricing.add_tax" in text


def test_tool_search(index: Path) -> None:
    text = tool_search(index, "tax")
    assert "pricing.add_tax" in text
    assert "orchestrator.quote" not in text


def test_tool_search_no_hits(index: Path) -> None:
    assert "no components match" in tool_search(index, "zzz").lower()


def test_tool_entrypoints_empty_on_fixture(index: Path) -> None:
    assert "no entrypoints" in tool_entrypoints(index).lower()


def test_tool_verify(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pricing.py").write_text("def add_tax(price, rate):\n    return price * (1 + rate)\n")
    out = tmp_path / "idx"
    assert CliRunner().invoke(app, ["scan", str(repo), "--out", str(out)]).exit_code == 0
    text = tool_verify(
        out,
        repo,
        "pricing.add_tax",
        "def add_tax(price, rate):\n    print(price)\n    return price\n",
    )
    assert "CHANGED" in text
    assert "effects" in text


def test_create_server_requires_optional_dependency() -> None:
    try:
        import mcp  # noqa: F401

        pytest.skip("mcp installed; missing-dependency path not reachable")
    except ImportError:
        pass
    from cgir.api.mcp_server import create_server

    with pytest.raises(RuntimeError, match=r"cgir\[mcp\]"):
        create_server(Path("."))
