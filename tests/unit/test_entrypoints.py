"""RED-phase tests for entrypoint detection (Sprint 17).

Contract:

* ``detect(decorators, func_name) -> str | None`` — pure over the
  decorator texts recorded by the ingester (no ``@``, full call text).
* FastAPI/Flask-style HTTP routes give ``"HTTP <METHOD> <path>"``;
  typer/click commands give ``"CLI <func>"``; celery tasks give
  ``"task <func>"``. Ordinary decorators (@property, @staticmethod,
  custom) give ``None``.
"""

from __future__ import annotations

from cgir.analyses.entrypoints import detect


def test_fastapi_router_get() -> None:
    assert detect(['router.get("/novels/{novel_id}")'], "get_novel") == (
        "HTTP GET /novels/{novel_id}"
    )


def test_fastapi_app_post_with_kwargs() -> None:
    assert detect(['app.post("/chapters", response_model=Chapter)'], "create") == (
        "HTTP POST /chapters"
    )


def test_flask_route() -> None:
    assert detect(['app.route("/health")'], "health") == "HTTP /health"


def test_typer_command() -> None:
    assert detect(["app.command()"], "scan") == "CLI scan"


def test_click_command() -> None:
    assert detect(["click.command()"], "main") == "CLI main"


def test_celery_shared_task() -> None:
    assert detect(["shared_task"], "send_email") == "task send_email"


def test_plain_decorators_are_not_entrypoints() -> None:
    assert detect(["property"], "x") is None
    assert detect(["staticmethod"], "x") is None
    assert detect(["functools.lru_cache(maxsize=1)"], "x") is None


def test_first_matching_decorator_wins() -> None:
    assert detect(["property", 'router.delete("/x")'], "rm") == "HTTP DELETE /x"


def test_no_decorators() -> None:
    assert detect([], "f") is None
