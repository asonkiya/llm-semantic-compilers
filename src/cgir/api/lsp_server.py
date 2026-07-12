"""LSP diagnostics — live contract drift as editor squiggles.

Diagnostics-only language server (no completion, no hover): on every save
it rescans the repo, and publishes

* **errors** for pin violations (`# cgir: pure` that isn't) — absolute
  invariants, visible from the first refresh;
* **warnings** for gate drift vs the previous scan (the default low-noise
  rule set: effect gain/loss on net/fs/db, always-on change pins).

The pure core (:func:`compute_diagnostics`, :class:`DiagnosticsEngine`) is
fully testable offline; pygls wiring hides behind the ``cgir[lsp]`` extra
(lazy import — same pattern as mcp/llm). A useful side effect: the engine
writes the ``.cgir`` index on every refresh, so MCP/pack/impact stay fresh
while you edit.

Run: ``cgir lsp`` (stdio). Editor config in ``docs/lsp.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgir.hooks import DEFAULT_FAIL_ON
from cgir.ir.component_spec import ComponentSpec
from cgir.pipeline import scan_repo
from cgir.report.diff import compute_diff, violations
from cgir.report.pins import change_violations, state_violations


@dataclass(slots=True)
class Diagnostic:
    path: str  # repo-relative
    line: int  # 1-based
    severity: str  # "error" | "warning"
    message: str


def _locate(specs: list[ComponentSpec]) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for spec in specs:
        if spec.trace:
            path, _, line = spec.trace[0].rpartition(":")
            try:
                out[spec.id] = (path, int(line))
            except ValueError:
                continue
    return out


def _place(
    lines: list[str], located: dict[str, tuple[str, int]], severity: str
) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    for entry in lines:
        spec_id, _, message = entry.partition(": ")
        hit = located.get(spec_id)
        if hit is None or not message:
            continue  # can't be placed in a file — skip rather than guess
        path, line = hit
        diags.append(Diagnostic(path=path, line=line, severity=severity, message=message))
    return diags


def compute_diagnostics(
    old_specs: list[ComponentSpec],
    new_specs: list[ComponentSpec],
    rules: tuple[str, ...] = DEFAULT_FAIL_ON,
) -> list[Diagnostic]:
    """Pin violations (errors) + gate drift vs the previous scan (warnings)."""
    located = _locate(new_specs)
    diags = _place(state_violations(new_specs), located, "error")
    diags += _place(change_violations(old_specs, new_specs), located, "error")
    if old_specs:
        diff = compute_diff(old_specs, new_specs)
        diags += _place(violations(diff, list(rules)), located, "warning")
    return diags


class DiagnosticsEngine:
    """Rescan-on-save loop: keeps the previous scan for drift comparison."""

    def __init__(self, repo: Path, index_dir: Path | None = None) -> None:
        self._repo = repo
        self._index_dir = index_dir or (repo / ".cgir")
        self._prev: list[ComponentSpec] = []
        self._primed = False

    def refresh(self) -> list[Diagnostic]:
        result = scan_repo(self._repo, out=self._index_dir)
        old = self._prev if self._primed else []
        diags = compute_diagnostics(old, result.specs)
        self._prev = result.specs
        self._primed = True
        return diags


def create_server() -> Any:
    """Build the pygls server (requires the ``cgir[lsp]`` extra)."""
    try:
        from lsprotocol import types as lsp

        try:  # pygls 2.x
            from pygls.lsp.server import LanguageServer
        except ImportError:  # pygls 1.x
            from pygls.server import LanguageServer  # type: ignore[attr-defined,no-redef]
    except ImportError as exc:
        raise RuntimeError(
            "Install cgir[lsp] to run the language server (adds the pygls package)"
        ) from exc

    server = LanguageServer("cgir", "0")
    state: dict[str, Any] = {"engine": None, "published": set()}

    _SEVERITY = {
        "error": lsp.DiagnosticSeverity.Error,
        "warning": lsp.DiagnosticSeverity.Warning,
    }

    def _publish(diags: list[Diagnostic]) -> None:
        engine: DiagnosticsEngine = state["engine"]
        by_path: dict[str, list[Diagnostic]] = {}
        for d in diags:
            by_path.setdefault(d.path, []).append(d)
        current = set(by_path)
        for rel in current | state["published"]:
            uri = (engine._repo / rel).resolve().as_uri()
            items = [
                lsp.Diagnostic(
                    range=lsp.Range(
                        start=lsp.Position(line=max(d.line - 1, 0), character=0),
                        end=lsp.Position(line=max(d.line - 1, 0), character=200),
                    ),
                    message=d.message,
                    severity=_SEVERITY[d.severity],
                    source="cgir",
                )
                for d in by_path.get(rel, [])
            ]
            _send(uri, items)
        state["published"] = current

    def _send(uri: str, items: list[Any]) -> None:
        if hasattr(server, "publish_diagnostics"):  # pygls 1.x
            server.publish_diagnostics(uri, items)
        else:  # pygls 2.x
            server.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(uri=uri, diagnostics=items)
            )

    @server.feature(lsp.INITIALIZED)
    def _initialized(_params: Any) -> None:
        root = server.workspace.root_path
        if root:
            state["engine"] = DiagnosticsEngine(Path(root))
            _publish(state["engine"].refresh())

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    def _did_save(_params: Any) -> None:
        if state["engine"] is not None:
            _publish(state["engine"].refresh())

    return server


__all__ = ["Diagnostic", "DiagnosticsEngine", "compute_diagnostics", "create_server"]
