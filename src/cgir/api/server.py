"""FastAPI surface (P1 stub).

Only available with ``pip install cgir[api]``. Every route returns 501 with
the milestone tag so the surface is observable but unimplemented.
"""

from __future__ import annotations

from typing import Any


def create_app() -> Any:
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        raise RuntimeError("Install cgir[api] to use the HTTP surface") from exc

    app = FastAPI(title="CGIR", version="0.0.1")

    def _not_yet() -> None:
        raise HTTPException(status_code=501, detail="milestone: P1-api")

    @app.post("/scan")
    def scan() -> None:  # pragma: no cover - stub
        _not_yet()

    @app.get("/components/{component_id}")
    def get_component(component_id: str) -> None:  # pragma: no cover - stub
        _not_yet()

    @app.get("/trace")
    def trace(path: str, line: int) -> None:  # pragma: no cover - stub
        _not_yet()

    @app.post("/regenerate")
    def regenerate() -> None:  # pragma: no cover - stub
        _not_yet()

    return app
