"""FastAPI surface over the scan pipeline.

Only available with ``pip install cgir[api]``. Per ``docs/roadmap.md``,
the API is a thin surface over the same driver the CLI uses
(:func:`cgir.pipeline.scan_repo`) — no separate pipeline.

The app is bound to an index directory (default ``.cgir``); a successful
``POST /scan`` re-points it at the freshly written index. Reads against a
missing index answer 409 rather than 404 so "no scan yet" is
distinguishable from "unknown component".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cgir.export.json_export import read_specs
from cgir.pipeline import scan_repo
from cgir.regenerate import regenerate as run_regenerate
from cgir.report.stats import compute_stats
from cgir.trace import TraceMap


class ScanRequest(BaseModel):
    repo: str
    out: str | None = None
    exclude: list[str] = []


class RegenerateRequest(BaseModel):
    component_id: str
    lang: str = "typescript"


def create_app(index_dir: Path | None = None) -> Any:
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        raise RuntimeError("Install cgir[api] to use the HTTP surface") from exc

    app = FastAPI(title="CGIR", version="0.1.0")
    state: dict[str, Path] = {"index_dir": index_dir or Path(".cgir")}

    def _index() -> Path:
        current = state["index_dir"]
        if not (current / "components").is_dir():
            raise HTTPException(status_code=409, detail=f"No index at {current}; POST /scan first")
        return current

    def _spec_path(component_id: str) -> Path:
        path = _index() / "components" / f"{component_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Unknown component: {component_id}")
        return path

    @app.post("/scan")
    def scan(req: ScanRequest) -> dict[str, Any]:
        repo = Path(req.repo)
        if not repo.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {repo}")
        result = scan_repo(repo, Path(req.out) if req.out else None, req.exclude)
        state["index_dir"] = result.out_dir
        return {"out_dir": str(result.out_dir), "components": len(result.specs)}

    @app.get("/components")
    def list_components() -> list[dict[str, Any]]:
        index_path = _index() / "components_index.json"
        entries: list[dict[str, Any]] = json.loads(index_path.read_text())
        return entries

    @app.get("/components/{component_id}")
    def get_component(component_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(_spec_path(component_id).read_text())
        return payload

    @app.get("/trace")
    def trace(path: str, line: int) -> dict[str, str]:
        trace_path = _index() / "trace_map.json"
        if not trace_path.exists():
            raise HTTPException(status_code=409, detail=f"No trace map at {trace_path}")
        hit = TraceMap.read(trace_path).lookup(path, line)
        if hit is None:
            raise HTTPException(status_code=404, detail=f"No component covers {path}:{line}")
        return {"component_id": hit}

    @app.post("/regenerate")
    def regenerate(req: RegenerateRequest) -> dict[str, str]:
        from cgir.ir.component_spec import ComponentSpec

        spec = ComponentSpec.from_dict(json.loads(_spec_path(req.component_id).read_text()))
        result = run_regenerate(spec, req.lang)
        return {
            "component_id": result.spec_id,
            "lang": result.target_language,
            "prompt": result.prompt,
            "code": result.code,
        }

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        return compute_stats(read_specs(_index()))

    return app
