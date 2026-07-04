"""RED-phase tests for the HTTP API (milestone: P1-api).

Contract (per docs/roadmap.md: the API calls the same pipeline functions
as the CLI — no separate driver):

* ``create_app(index_dir=None)`` returns a FastAPI app bound to an index
  directory (default ``.cgir``).
* ``POST /scan`` ``{"repo": ..., "out": ...}`` runs the full pipeline,
  points the app at the new index, and returns component counts.
* ``GET /components`` lists index entries; ``GET /components/{id}``
  returns one ComponentSpec (404 if unknown).
* ``GET /trace?path=..&line=..`` resolves a source location (404 on miss).
* ``POST /regenerate`` ``{"component_id": ..., "lang": ...}`` returns the
  prompt-pack + (stub) code.
* ``GET /stats`` returns the structure report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from cgir.api.server import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path: Path, python_sample_repo: Path) -> TestClient:
    """App pointed at a freshly scanned fixture index."""
    app = create_app()
    client = TestClient(app)
    resp = client.post(
        "/scan",
        json={"repo": str(python_sample_repo), "out": str(tmp_path / "idx")},
    )
    assert resp.status_code == 200, resp.text
    return client


def test_component_listing_matches_fixture(client: TestClient) -> None:
    listing = client.get("/components")
    assert listing.status_code == 200
    ids = {entry["id"] for entry in listing.json()}
    assert ids == {"pricing.add_tax", "orchestrator.quote"}


def test_get_component_returns_spec(client: TestClient) -> None:
    resp = client.get("/components/pricing.add_tax")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["id"] == "pricing.add_tax"
    assert spec["inputs"] == ["price", "rate"]


def test_get_component_404_for_unknown(client: TestClient) -> None:
    assert client.get("/components/nope.nothing").status_code == 404


def test_trace_resolves_location(client: TestClient) -> None:
    resp = client.get("/trace", params={"path": "pricing.py", "line": 1})
    assert resp.status_code == 200
    assert resp.json()["component_id"] == "pricing.add_tax"


def test_trace_404_on_miss(client: TestClient) -> None:
    resp = client.get("/trace", params={"path": "pricing.py", "line": 9999})
    assert resp.status_code == 404


def test_regenerate_returns_prompt_and_code(client: TestClient) -> None:
    resp = client.post(
        "/regenerate",
        json={"component_id": "pricing.add_tax", "lang": "typescript"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "pricing.add_tax" in body["prompt"]
    assert body["code"]


def test_stats_reports_structure(client: TestClient) -> None:
    resp = client.get("/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total"] == 2
    assert stats["kinds"]["pure_function"] == 2


def test_unscanned_index_is_conflict() -> None:
    """Before any scan (and with no .cgir), component routes answer 409."""
    app = create_app(index_dir=Path("/nonexistent-cgir-index"))
    client = TestClient(app)
    assert client.get("/components").status_code == 409
