"""MCP server — the semantic index as agent tools.

Agents are CGIR's target user, and agents call tools rather than reading
dashboards. The ``tool_*`` functions below are plain callables over an
index directory, each returning a string; :func:`create_server` wraps
them in FastMCP behind a lazy import (``pip install cgir[mcp]``),
mirroring the anthropic pattern in :mod:`cgir.regenerate.regenerator`.

Run it: ``cgir mcp --index .cgir`` (stdio transport), then register the
command as an MCP server in your agent's config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cgir.export.json_export import read_specs
from cgir.report.flow import render_flow
from cgir.report.impact import render_impact
from cgir.report.pack import build_pack, render_pack
from cgir.report.stats import compute_stats, render_text


def tool_stats(index_dir: Path) -> str:
    """Structure report: kinds, purity, effects, hotspots, entrypoints."""
    return render_text(compute_stats(read_specs(index_dir)))


def tool_component(index_dir: Path, component_id: str) -> str:
    """One ComponentSpec as JSON."""
    spec_path = index_dir / "components" / f"{component_id}.json"
    if not spec_path.exists():
        return f"unknown component: {component_id}"
    return spec_path.read_text()


def tool_flow(index_dir: Path, component_id: str, depth: int = 3) -> str:
    """Upstream callers and downstream callees, annotated."""
    try:
        return render_flow(read_specs(index_dir), component_id, depth)
    except KeyError:
        return f"unknown component: {component_id}"


def tool_impact(index_dir: Path, component_id: str) -> str:
    """Blast radius of changing a component: affected callers, entrypoints, tests."""
    try:
        return render_impact(read_specs(index_dir), component_id)
    except KeyError:
        return f"unknown component: {component_id}"


def tool_pack(index_dir: Path, component_id: str, budget: int = 4000) -> str:
    """The minimal context bundle for working on one component."""
    try:
        bundle = build_pack(read_specs(index_dir), component_id, budget=budget)
    except KeyError:
        return f"unknown component: {component_id}"
    return render_pack(bundle)


def tool_search(index_dir: Path, query: str) -> str:
    """Components whose id, entrypoint, or effects match a substring."""
    needle = query.lower()
    hits: list[str] = []
    for spec in read_specs(index_dir):
        haystack = " ".join([spec.id, spec.entrypoint or "", " ".join(spec.effects)]).lower()
        if needle in haystack:
            line = f"{spec.id}  [{spec.kind.value}]"
            if spec.entrypoint:
                line += f"  ({spec.entrypoint})"
            hits.append(line)
    if not hits:
        return f"no components match {query!r}"
    return "\n".join(hits) + "\n"


def tool_entrypoints(index_dir: Path) -> str:
    """The repo's external surface: HTTP routes, CLI commands, tasks."""
    entries = [s for s in read_specs(index_dir) if s.entrypoint]
    if not entries:
        return "no entrypoints detected"
    entries.sort(key=lambda s: (s.entrypoint or "", s.id))
    return "\n".join(f"{s.entrypoint}  {s.id}" for s in entries) + "\n"


def tool_verify(index_dir: Path, repo: Path, component_id: str, candidate: str) -> str:
    """Contract-check a candidate implementation before proposing it."""
    from cgir.verify import verify

    try:
        result = verify(index_dir, component_id, candidate, repo)
    except KeyError:
        return f"unknown component: {component_id}"
    lines = [f"contract: {'ok' if result.contract_ok else 'CHANGED'}"]
    for name, values in result.drift.items():
        lines.append(f"  {name}: {values['old']} -> {values['new']}")
    return "\n".join(lines) + "\n"


def create_server(index_dir: Path) -> Any:
    """Build the FastMCP server (requires the ``cgir[mcp]`` extra)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "Install cgir[mcp] to serve the index over MCP (adds the mcp package)"
        ) from exc

    server = FastMCP("cgir")

    @server.tool()
    def stats() -> str:
        """Codebase structure report: kinds, purity, effects, hotspots."""
        return tool_stats(index_dir)

    @server.tool()
    def component(component_id: str) -> str:
        """Get one ComponentSpec (contract: inputs/outputs/effects/calls) as JSON."""
        return tool_component(index_dir, component_id)

    @server.tool()
    def flow(component_id: str, depth: int = 3) -> str:
        """Trace a component: upstream callers and downstream callees."""
        return tool_flow(index_dir, component_id, depth)

    @server.tool()
    def impact(component_id: str) -> str:
        """Before editing a component, see its blast radius: which callers are affected,
        which entrypoints are at risk, and exactly which tests to run."""
        return tool_impact(index_dir, component_id)

    @server.tool()
    def pack(component_id: str, budget: int = 4000) -> str:
        """Minimal context bundle (spec, callee interfaces, callers) for editing a component."""
        return tool_pack(index_dir, component_id, budget)

    @server.tool()
    def search(query: str) -> str:
        """Find components by id / entrypoint / effect substring."""
        return tool_search(index_dir, query)

    @server.tool()
    def entrypoints() -> str:
        """The repo's external surface: HTTP routes, CLI commands, tasks."""
        return tool_entrypoints(index_dir)

    @server.tool()
    def verify(repo: str, component_id: str, candidate: str) -> str:
        """Contract-check a candidate implementation of a component before proposing it."""
        return tool_verify(index_dir, Path(repo), component_id, candidate)

    return server
