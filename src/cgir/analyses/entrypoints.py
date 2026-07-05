"""Entrypoint detection from decorator texts (Sprint 17).

The ingester records each decorated definition's decorator texts (sans
``@``); :func:`detect` maps them to a human-readable entrypoint label:

* ``router.get("/x")`` / ``app.post(...)`` → ``HTTP GET /x`` (FastAPI style)
* ``app.route("/x")`` → ``HTTP /x`` (Flask style; method set not parsed)
* ``app.command()`` / ``click.command()`` → ``CLI <func>`` (typer/click)
* ``shared_task`` / ``celery.task`` → ``task <func>``

Purely lexical — a decorator named like a router that isn't one will
false-positive; per spec, flag rather than solve. Framework dispatch is
exactly the dynamic behavior static call graphs can't see, so these
labels are how CGIR shows "called by the framework".
"""

from __future__ import annotations

import re

_HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "websocket"}
)
_HTTP_RECEIVERS: frozenset[str] = frozenset({"app", "router", "api", "blueprint", "bp"})
_TASK_MARKERS: frozenset[str] = frozenset({"task", "shared_task"})

_FIRST_STR_ARG = re.compile(r"\(\s*[rbf]*[\"']([^\"']*)[\"']")


def detect(decorators: list[str], func_name: str) -> str | None:
    """Map decorator texts to an entrypoint label, or None."""
    for decorator in decorators:
        head = decorator.split("(", 1)[0].strip()
        parts = head.split(".")
        if len(parts) == 2 and parts[0] in _HTTP_RECEIVERS:
            if parts[1] in _HTTP_METHODS:
                path = _first_str_arg(decorator)
                return (
                    f"HTTP {parts[1].upper()} {path}".rstrip()
                    if path
                    else (f"HTTP {parts[1].upper()}")
                )
            if parts[1] == "route":
                path = _first_str_arg(decorator)
                return f"HTTP {path}" if path else "HTTP"
        if parts[-1] == "command" and len(parts) >= 2:
            return f"CLI {func_name}"
        if parts[-1] in _TASK_MARKERS:
            return f"task {func_name}"
    return None


def _first_str_arg(decorator: str) -> str | None:
    match = _FIRST_STR_ARG.search(decorator)
    return match.group(1) if match else None
