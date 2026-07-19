"""CLI entry point — matches the command shape in Code-IR.md §Analysis/workflow.

The scan pipeline itself lives in :mod:`cgir.pipeline`; this module (and the
HTTP API) are thin surfaces over it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer

from cgir.analyses import param_flow
from cgir.export import graphml as graphml_export
from cgir.export import html_viz
from cgir.export.json_export import read_specs
from cgir.export.mermaid import render_call_graph
from cgir.ir.component_spec import ComponentSpec
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind
from cgir.manifest import compatibility_warning, read_manifest
from cgir.pipeline import scan_repo
from cgir.regenerate import regenerate as run_regenerate
from cgir.report.diff import compute_diff, render_diff, render_diff_markdown, violations
from cgir.report.flow import render_flow
from cgir.report.impact import (
    compute_impact,
    compute_typed_impact,
    render_impact,
    render_typed_impact,
)
from cgir.report.pack import build_pack, render_pack
from cgir.report.stats import compute_stats, render_text
from cgir.trace import TraceMap

app = typer.Typer(
    add_completion=False,
    help="CodeGraph IR - semantic IR for repo-scale LLM rewriting.",
)

hook_app = typer.Typer(help="Git pre-commit seatbelt: contract-check staged changes.")
app.add_typer(hook_app, name="hook")


@hook_app.command("run")
def hook_run(
    fail_on: Annotated[
        list[str] | None,
        typer.Option("--fail-on", help="Drift rules that block the commit (repeatable)."),
    ] = None,
) -> None:
    """Contract-check the staged tree against HEAD (invoked by the installed hook)."""
    from cgir.hooks import render_hook, run_check

    result = run_check(Path.cwd(), fail_on)
    typer.echo(render_hook(result), nl=False)
    if result.violations:
        raise typer.Exit(code=1)


@hook_app.command("install")
def hook_install(
    fail_on: Annotated[
        list[str] | None,
        typer.Option("--fail-on", help="Drift rules to bake into the hook (repeatable)."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing pre-commit hook.")
    ] = False,
) -> None:
    """Install the pre-commit seatbelt into this repo's .git/hooks."""
    from cgir.hooks import install

    try:
        path = install(Path.cwd(), fail_on, force)
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Installed contract seatbelt at {path}")


@hook_app.command("uninstall")
def hook_uninstall() -> None:
    """Remove the pre-commit seatbelt (only if CGIR installed it)."""
    from cgir.hooks import uninstall

    typer.echo("Removed contract seatbelt." if uninstall(Path.cwd()) else "No CGIR hook to remove.")


def _version_callback(value: bool) -> None:
    if value:
        from cgir import __version__

        typer.echo(f"cgir {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """CodeGraph IR — semantic IR for repo-scale LLM rewriting."""


@app.command()
def scan(
    repo: Annotated[Path, typer.Argument(exists=True, file_okay=False, dir_okay=True)],
    out: Annotated[
        Path | None, typer.Option("--out", help="Output directory (default <repo>/.cgir)")
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Additional directory names to skip during ingest (repeatable).",
        ),
    ] = None,
) -> None:
    """Scan a repository and write the RepoGraph + ComponentSpec index."""
    result = scan_repo(repo, out, exclude)
    typer.echo(f"Wrote {len(result.specs)} components to {result.out_dir}")
    _print_kind_histogram(result.specs)


def _print_kind_histogram(specs: list[ComponentSpec]) -> None:
    if not specs:
        return
    counts = Counter(spec.kind.value for spec in specs)
    # Stable display order: pure → orchestrator → state → effect → unknown.
    order = ["pure_function", "orchestrator", "state_transformer", "effect_adapter", "unknown"]
    for kind in order:
        if counts.get(kind):
            typer.echo(f"  {kind}: {counts[kind]}")
    for kind, n in counts.items():
        if kind not in order:
            typer.echo(f"  {kind}: {n}")


@app.command()
def export(
    fmt: Annotated[str, typer.Option("--format", help="One of: json | graphml | neo4j")] = "json",
    out: Annotated[Path, typer.Option("--out")] = Path(".cgir"),
) -> None:
    """Re-export an existing index."""
    if fmt == "json":
        typer.echo(f"JSON outputs already at {out}; nothing to do.")
        return
    if fmt == "graphml":
        path = graphml_export.write(out, _load_graph(out))
        typer.echo(f"Wrote {path}")
        return
    if fmt == "neo4j":
        raise NotImplementedError("milestone: P2-neo4j")
    raise typer.BadParameter(f"Unknown format: {fmt}")


@app.command()
def viz(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    fmt: Annotated[str, typer.Option("--format", help="One of: html | mermaid")] = "html",
) -> None:
    """Render the component graph — a self-contained HTML page or Mermaid text."""
    specs = _load_specs(index_dir)
    if fmt == "html":
        path = html_viz.write(index_dir, specs, arg_flows=_arg_flows(index_dir))
        typer.echo(f"Wrote {path} — open it in a browser.")
    elif fmt == "mermaid":
        typer.echo(render_call_graph(specs), nl=False)
    else:
        raise typer.BadParameter(f"Unknown format: {fmt}")


@app.command()
def stats(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Summarize the scanned codebase: kinds, purity, effects, hotspots."""
    result = compute_stats(_load_specs(index_dir))
    if as_json:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        typer.echo(render_text(result), nl=False)


_STARTER_CONFIG = """\
# CGIR architecture rules — see docs/architecture-rules.md.
# Pins (`# cgir: pure`, `no-net`, `stable-signature`, `frozen`) need no config;
# they are enforced by `cgir lint`, the pre-commit hook, and `cgir diff`.
#
# [[rule]]
# name = "core stays pure"
# in = "app.core.*"
# forbid-effect = ["net", "db"]
#
# [[rule]]
# name = "no call cycles"
# forbid-cycle = true
#
# [[rule]]
# name = "layered"
# layers = ["app.api.*", "app.core.*", "app.db.*"]
"""

_NEXT_STEPS = """\
Next steps:
  cgir watch {repo}                 # live index + contract drift on save
  cgir hook install                 # pre-commit seatbelt (or: cgir init --hook)
  cgir mcp --index {index}          # serve the index to your agent over MCP
  CI gate: see docs/github-action.md
"""


@app.command()
def init(
    repo: Annotated[Path, typer.Argument(exists=True, file_okay=False)] = Path("."),
    hook: Annotated[
        bool, typer.Option("--hook", help="Also install the pre-commit seatbelt.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing cgir.toml.")
    ] = False,
) -> None:
    """One-command onboarding: scan, starter config, .gitignore, next steps."""
    result = scan_repo(repo, repo / ".cgir")
    typer.echo(f"Indexed {len(result.specs)} components:")
    _print_kind_histogram(result.specs)
    entrypoints = [s for s in result.specs if s.entrypoint]
    if entrypoints:
        typer.echo(f"  entrypoints: {len(entrypoints)}")
    untested = [s for s in result.specs if s.effects and not s.covered_by]
    if untested:
        typer.echo(f"  effectful without linked tests: {len(untested)}")

    config = repo / "cgir.toml"
    if config.exists() and not force:
        typer.echo(f"kept existing {config.name}")
    else:
        config.write_text(_STARTER_CONFIG)
        typer.echo(f"wrote {config.name} (report-only starter)")

    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if ".cgir/" not in existing:
        gitignore.write_text(
            existing + ("" if existing.endswith("\n") or not existing else "\n") + ".cgir/\n"
        )
        typer.echo("added .cgir/ to .gitignore")

    if hook:
        from cgir.hooks import install as install_hook

        try:
            path = install_hook(repo)
            typer.echo(f"installed contract seatbelt at {path}")
        except FileExistsError:
            typer.echo("pre-commit hook already exists — skipped (cgir hook install --force)")

    typer.echo("")
    typer.echo(_NEXT_STEPS.format(repo=repo, index=repo / ".cgir"), nl=False)


@app.command()
def watch(
    repo: Annotated[Path, typer.Argument(exists=True, file_okay=False)] = Path("."),
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    interval: Annotated[float, typer.Option("--interval", help="Poll interval (s).")] = 0.5,
    once: Annotated[bool, typer.Option("--once", help="Run a single tick and exit.")] = False,
) -> None:
    """Keep the index live: on each save, re-scan and print contract drift."""
    from cgir.watch import run_watch

    if not once:
        typer.echo(f"watching {repo.resolve()} → {index_dir} (Ctrl-C to stop)")
    try:
        run_watch(repo, index_dir, interval=interval, once=once, emit=typer.echo)
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


@app.command()
def search(
    query: Annotated[
        str, typer.Argument(help="Free terms + predicates (kind:pure effects:net callers:>3 ...)")
    ],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Ranked component search over the index (contract predicates included)."""
    from cgir.report.search import render_search

    typer.echo(render_search(_load_specs(index_dir), query), nl=False)


@app.command()
def flow(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    depth: Annotated[int, typer.Option("--depth", help="Max hops in each direction.")] = 3,
) -> None:
    """Trace a component: upstream callers, downstream callees, constructed types."""
    specs = _load_specs(index_dir)
    try:
        typer.echo(render_flow(specs, component_id, depth), nl=False)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown component: {component_id}") from exc


@app.command()
def impact(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    changed: Annotated[
        str | None,
        typer.Option(
            "--changed",
            help="Comma-separated contract fields that changed "
            "(effects,purity,kind,signature,outputs) — narrows the radius.",
        ),
    ] = None,
    candidate: Annotated[
        Path | None,
        typer.Option(
            "--candidate",
            exists=True,
            help="A proposed new implementation; its contract delta (via verify) "
            "narrows the radius. Requires --repo.",
        ),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo root (with --candidate or --run)."),
    ] = None,
    run: Annotated[
        bool,
        typer.Option(
            "--run", help="Execute the selected tests (pytest) and exit with their status."
        ),
    ] = False,
) -> None:
    """Blast radius of changing a component: affected callers, entrypoints at risk, tests to run.

    Worst-case by default; narrowed by --changed or by a --candidate's actual contract delta.
    --run executes exactly the tests the radius names.
    """
    specs = _load_specs(index_dir)
    try:
        if candidate is not None:
            if repo is None:
                raise typer.BadParameter("--candidate requires --repo")
            from cgir.verify import verify

            result = verify(index_dir, component_id, candidate.read_text(), repo)
            delta = list(result.drift.keys())
            data = compute_typed_impact(specs, component_id, delta)
            renderer = render_typed_impact(specs, component_id, delta)
        elif changed is not None:
            delta = [f.strip() for f in changed.split(",") if f.strip()]
            data = compute_typed_impact(specs, component_id, delta)
            renderer = render_typed_impact(specs, component_id, delta)
        else:
            data = compute_impact(specs, component_id)
            renderer = render_impact(specs, component_id)
        typer.echo(
            json.dumps(data, indent=2, sort_keys=True) if as_json else renderer, nl=not as_json
        )
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown component: {component_id}") from exc
    if run:
        _run_impact_tests(specs, list(data["tests"]), repo or Path.cwd())


def _run_impact_tests(specs: list[ComponentSpec], test_ids: list[str], repo: Path) -> None:
    """Execute the impact-selected tests with pytest; exit with their status."""
    import subprocess
    import sys

    from cgir.report.impact import runnable_selectors

    selectors, skipped = runnable_selectors(specs, test_ids)
    for test_id in skipped:
        typer.echo(f"  (skipped, not pytest-runnable: {test_id})")
    if not selectors:
        typer.echo("no runnable tests selected.")
        return
    typer.echo(f"running {len(selectors)} test(s):")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *selectors],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    typer.echo(proc.stdout, nl=False)
    if proc.stderr:
        typer.echo(proc.stderr, nl=False)
    if proc.returncode != 0:
        raise typer.Exit(code=proc.returncode)


@app.command()
def pack(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo root, for embedding the target's source."),
    ] = None,
    budget: Annotated[int, typer.Option("--budget", help="Approximate token budget.")] = 4000,
) -> None:
    """Emit the minimal context bundle for working on one component."""
    from cgir.report.pack import referenced_type_names

    specs = _load_specs(index_dir)
    target = next((s for s in specs if s.id == component_id), None)
    if target is None:
        raise typer.BadParameter(f"Unknown component: {component_id}")

    graph = _load_graph(index_dir) if (index_dir / "repo_graph.json").exists() else None
    source = _component_source(graph, component_id, repo) if repo else None
    types = _type_sources(graph, referenced_type_names(target), repo) if repo else {}
    # Coverage-grounded linkage can attach dozens of tests to hot-path
    # components; embed at most a handful of sources (impact --run still
    # uses the full set — accuracy there, budget here).
    tests = _test_sources(graph, target.covered_by[:_PACK_TEST_CAP], repo) if repo else {}
    context = _module_context(graph, component_id, repo) if repo else {}
    receivers = _call_receivers(graph, target)
    bundle = build_pack(
        specs,
        component_id,
        source=source,
        budget=budget,
        types=types,
        tests=tests,
        context=context,
        receivers=receivers,
    )
    typer.echo(render_pack(bundle), nl=False)


_HELPER_MAX_LINES = 25
_PACK_TEST_CAP = 5


def _module_context(
    graph: RepoGraph | None, component_id: str, repo: Path | None
) -> dict[str, str]:
    """Same-module constants and small helpers the target's body references."""
    if graph is None or repo is None:
        return {}
    target = next(
        (
            n
            for n in graph.nodes()
            if n.kind in {NodeKind.Function, NodeKind.Method}
            and n.attrs.get("qualname") == component_id
        ),
        None,
    )
    if target is None:
        return {}
    free = target.attrs.get("free_names")
    if not isinstance(free, list) or not free:
        return {}
    module = component_id.rsplit(".", 1)[0]
    wanted = {f"{module}.{name}" for name in free}
    out: dict[str, str] = {}
    for node in graph.nodes():
        qual = node.attrs.get("qualname")
        if not isinstance(qual, str) or qual not in wanted or qual == component_id:
            continue
        if node.kind == NodeKind.Variable:
            src = _span_source(node, repo)
        elif node.kind in {NodeKind.Function, NodeKind.Method}:
            span = (node.end_line or 0) - (node.start_line or 0)
            src = _span_source(node, repo) if span <= _HELPER_MAX_LINES else None
        else:
            continue
        if src:
            out[qual.rsplit(".", 1)[-1]] = src
    return out


def _call_receivers(graph: RepoGraph | None, target: ComponentSpec) -> dict[str, str]:
    """Map each DI callee to the field it is reached through.

    The target's owning class records injected/declared fields as
    ``{field: TypeName}``. A callee whose class matches one of those field
    types is called via that field; surfacing it lets a rewriter reproduce
    the call — and preserve the effect contract — instead of guessing the
    field name. The receiver keyword is language-aware: ``self.<field>`` in
    Python, ``this.<field>`` in TypeScript. Empty for classes without
    fields, so the pack is unchanged there.
    """
    if graph is None:
        return {}
    self_kw = "self" if target.language == "python" else "this"
    class_qual = target.id.rsplit(".", 1)[0]
    cls = next(
        (
            n
            for n in graph.nodes()
            if n.kind == NodeKind.Class and n.attrs.get("qualname") == class_qual
        ),
        None,
    )
    fields = cls.attrs.get("fields") if cls is not None else None
    if not isinstance(fields, dict) or not fields:
        return {}
    type_to_field: dict[str, str] = {}
    for field, type_name in fields.items():
        type_to_field.setdefault(type_name, field)
    out: dict[str, str] = {}
    for callee in target.calls:
        callee_class = callee.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        field = type_to_field.get(callee_class)
        if field:
            out[callee] = f"{self_kw}.{field}"
    return out


def _test_sources(
    graph: RepoGraph | None, test_ids: list[str], repo: Path | None
) -> dict[str, str]:
    """Resolve the target's linked test components to their source."""
    if graph is None or repo is None or not test_ids:
        return {}
    wanted = set(test_ids)
    out: dict[str, str] = {}
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        if node.attrs.get("qualname") not in wanted:
            continue
        src = _span_source(node, repo)
        if src:
            out[str(node.attrs.get("qualname"))] = src
    return out


def _component_source(graph: RepoGraph | None, component_id: str, repo: Path | None) -> str | None:
    """The target's source lines, via the graph's span (best-effort)."""
    if graph is None or repo is None:
        return None
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        if node.attrs.get("qualname") != component_id:
            continue
        return _span_source(node, repo)
    return None


def _type_sources(
    graph: RepoGraph | None, type_names: set[str], repo: Path | None
) -> dict[str, str]:
    """Resolve referenced type names to their in-repo Class definitions."""
    if graph is None or repo is None or not type_names:
        return {}
    out: dict[str, str] = {}
    for node in graph.nodes():
        if node.kind not in {NodeKind.Class, NodeKind.Variable}:
            continue
        if node.name not in type_names or node.name in out:
            continue  # first match wins; ambiguous names take one
        src = _span_source(node, repo)
        if src:
            out[node.name] = src
    return out


def _span_source(node: Any, repo: Path) -> str | None:
    if node.path is None or node.start_line is None or node.end_line is None:
        return None
    try:
        all_lines = (repo / node.path).read_text().splitlines()
    except OSError:
        return None
    return "\n".join(all_lines[node.start_line - 1 : node.end_line]) + "\n"


@app.command()
def lint(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    config: Annotated[Path, typer.Option("--config", help="Rules file.")] = Path("cgir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check the index against semantic architecture rules (effects, kind, calls).

    Pin invariants (`# cgir: pure`, `no-<tag>`) are always checked — no rules
    file needed for those.
    """
    from cgir.report.lint import lint as run_lint
    from cgir.report.lint import load_rules, render_lint
    from cgir.report.pins import state_violations

    specs = _load_specs(index_dir)
    violations = run_lint(specs, load_rules(config)) if config.exists() else []
    pin_lines = state_violations(specs)
    if as_json:
        typer.echo(
            json.dumps(
                [{"rule": v.rule, "component": v.component, "detail": v.detail} for v in violations]
                + [
                    {"rule": "pin", "component": line.split(":")[0], "detail": line}
                    for line in pin_lines
                ],
                indent=2,
            )
        )
    else:
        typer.echo(render_lint(violations), nl=False)
        if pin_lines:
            typer.echo(f"pin violations ({len(pin_lines)}):")
            for line in pin_lines:
                typer.echo(f"  ! {line}")
    if violations or pin_lines:
        raise typer.Exit(code=1)


@app.command()
def verify(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    candidate: Annotated[
        Path, typer.Option("--candidate", help="File with the new implementation.")
    ],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    repo: Annotated[Path, typer.Option("--repo", help="Repo root.")] = Path("."),
    fail_on: Annotated[
        list[str] | None,
        typer.Option("--fail-on", help="Drift rules that fail the check (repeatable)."),
    ] = None,
    run_tests: Annotated[
        bool, typer.Option("--tests", help="Also run the component's linked tests.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Contract-check an LLM-written candidate against the indexed component."""
    from cgir.verify import verify as run_verify

    try:
        result = run_verify(
            index_dir,
            component_id,
            candidate.read_text(),
            repo,
            fail_on=list(fail_on or []),
            run_tests=run_tests,
        )
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        typer.echo(f"contract: {'ok' if result.contract_ok else 'CHANGED'}")
        for name, values in result.drift.items():
            typer.echo(f"  {name}: {values['old']} -> {values['new']}")
        if result.violations:
            typer.echo("violations:")
            for line in result.violations:
                typer.echo(f"  ! {line}")
        if result.tests_ok is not None:
            typer.echo(
                f"tests ({len(result.tests_ran)} file(s)): {'pass' if result.tests_ok else 'FAIL'}"
            )
    if result.violations or result.tests_ok is False:
        raise typer.Exit(code=1)


@app.command()
def mcp(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Serve the index to agents over MCP (stdio; requires cgir[mcp])."""
    from cgir.api.mcp_server import create_server

    try:
        server = create_server(index_dir)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    server.run()


@app.command()
def languages() -> None:
    """List registered language adapters (builtins + installed plugins)."""
    from cgir.languages.base import ADAPTER_API_VERSION
    from cgir.languages.registry import _BUILTINS, _PLUGIN_WARNINGS, ADAPTERS

    builtin_names = {a.name for a in _BUILTINS}
    typer.echo(f"adapter api version: {ADAPTER_API_VERSION}")
    for name, adapter in sorted(ADAPTERS.items()):
        origin = "builtin" if name in builtin_names else "plugin"
        typer.echo(f"  {name:14} {', '.join(adapter.file_extensions):18} [{origin}]")
    for note in _PLUGIN_WARNINGS:
        typer.echo(f"  ! {note}")


@app.command()
def decompose(
    component_id: Annotated[str | None, typer.Argument(metavar="ID")] = None,
    repo: Annotated[Path, typer.Option("--repo", help="Repo root to analyze.")] = Path("."),
    all_: Annotated[bool, typer.Option("--all", help="Repo-wide decomposability report.")] = False,
    min_core: Annotated[int, typer.Option("--min-core", help="Minimum pure-core statements.")] = 3,
) -> None:
    """Suggest a functional-core/imperative-shell split for an impure component."""
    from cgir.analyses.call_graph import build_call_graph
    from cgir.analyses.cfg import build as build_cfg
    from cgir.analyses.decompose import decompose as run_decompose
    from cgir.analyses.decompose import decompose_all, render_decompose
    from cgir.analyses.effects import classify
    from cgir.analyses.pdg import build as build_pdg
    from cgir.analyses.symbols import build_symbol_tables
    from cgir.sources import TreeSitterSource

    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    build_cfg(graph, repo)
    build_pdg(graph)
    effects = classify(graph, repo)

    if all_:
        report = decompose_all(graph, effects, repo, min_core=min_core)
        typer.echo(
            f"impure functions: {report['impure_functions']}  "
            f"decomposable: {report['decomposable']}  "
            f"({report['decomposability_pct']}%)"
        )
        for r in report["results"]:
            if r.decomposable:
                typer.echo(f"  {r.function_id}: {r.core_statements}/{r.total_statements} core")
        return
    if component_id is None:
        raise typer.BadParameter("give a component id or --all")
    prefix_id = component_id if ":" in component_id else None
    target = prefix_id or next(
        (
            f"{p}:{component_id}"
            for p in ("func", "method")
            if graph.has_node(f"{p}:{component_id}")
        ),
        component_id,
    )
    try:
        result = run_decompose(graph, effects, target, repo, min_core=min_core)
    except Exception as exc:
        raise typer.BadParameter(f"cannot decompose {component_id}: {exc}") from exc
    typer.echo(render_decompose(result), nl=False)


@app.command()
def lsp() -> None:
    """Serve live contract diagnostics over LSP (stdio; requires cgir[lsp])."""
    from cgir.api.lsp_server import create_server

    try:
        server = create_server()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    server.start_io()


@app.command()
def diff(
    old_index: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    new_index: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Emit a PR-comment-ready markdown report.")
    ] = False,
    fail_on: Annotated[
        list[str] | None,
        typer.Option(
            "--fail-on",
            help="Exit 1 on drift: effect-gain[:tag] | effect-loss[:tag] | purity-drop | "
            "kind-change | entrypoint-added | entrypoint-change (repeatable).",
        ),
    ] = None,
) -> None:
    """Compare two scan indexes: added/removed components and contract drift.

    Pin invariants are always enforced: change pins (stable-signature, frozen)
    across the pair, state pins (pure, no-<tag>) on the new index.
    """
    from cgir.export.json_export import read_types
    from cgir.report.pins import change_violations, state_violations

    warning = compatibility_warning(read_manifest(old_index), read_manifest(new_index))
    old_specs, new_specs = _load_specs(old_index), _load_specs(new_index)
    result = compute_diff(
        old_specs, new_specs, old_types=read_types(old_index), new_types=read_types(new_index)
    )
    found = violations(result, list(fail_on or []))
    found += change_violations(old_specs, new_specs)
    found += state_violations(new_specs)
    if as_json:
        payload = dict(result)
        if warning:
            payload["warning"] = warning
        payload["violations"] = found
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif markdown:
        typer.echo(render_diff_markdown(result, violations=found, warning=warning), nl=False)
    else:
        if warning:
            typer.echo(warning)
        typer.echo(render_diff(result), nl=False)
        if found:
            typer.echo("")
            typer.echo(f"drift violations ({len(found)}):")
            for line in found:
                typer.echo(f"  ! {line}")
    if found:
        raise typer.Exit(code=1)


def _load_graph(index_dir: Path) -> RepoGraph:
    graph_path = index_dir / "repo_graph.json"
    if not graph_path.exists():
        raise typer.BadParameter(f"No graph at {graph_path}; run `cgir scan` first")
    return RepoGraph.from_jsonable(json.loads(graph_path.read_text()))


def _arg_flows(index_dir: Path) -> dict[str, list[dict[str, object]]] | None:
    """PDG-derived param→callee flows, re-keyed by spec id (qualname)."""
    graph_path = index_dir / "repo_graph.json"
    if not graph_path.exists():
        return None
    graph = RepoGraph.from_jsonable(json.loads(graph_path.read_text()))
    flows = param_flow.compute(graph)

    def qual(node_id: str) -> str:
        node = graph.get_node(node_id)
        q = node.attrs.get("qualname") if node.attrs else None
        return str(q) if isinstance(q, str) else node.name

    return {
        qual(caller): [
            {"callee": qual(str(entry["callee"])), "params": entry["params"]} for entry in entries
        ]
        for caller, entries in flows.items()
    }


def _load_specs(index_dir: Path) -> list[ComponentSpec]:
    if not (index_dir / "components").is_dir():
        raise typer.BadParameter(f"No components at {index_dir}; run `cgir scan` first")
    return read_specs(index_dir)


@app.command()
def component(
    component_id: Annotated[str, typer.Argument()],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Pretty-print a ComponentSpec."""
    spec_path = index_dir / "components" / f"{component_id}.json"
    if not spec_path.exists():
        raise typer.BadParameter(f"No spec at {spec_path}")
    typer.echo(spec_path.read_text())


@app.command()
def trace(
    location: Annotated[str, typer.Argument(help="path:line, e.g. pricing.py:1")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Look up which ComponentSpec owns a given source location."""
    if ":" not in location:
        raise typer.BadParameter("location must be <path>:<line>")
    path, line_str = location.rsplit(":", 1)
    line = int(line_str)
    trace_path = index_dir / "trace_map.json"
    if not trace_path.exists():
        raise typer.BadParameter(f"No trace map at {trace_path}; run `cgir scan` first")
    trace_map = TraceMap.read(trace_path)
    hit = trace_map.lookup(path, line)
    if hit is None:
        typer.echo("(no component covers that location)")
        raise typer.Exit(code=1)
    typer.echo(hit)


@app.command(name="rewrite")
def rewrite_cmd(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    repo: Annotated[Path, typer.Option("--repo", help="Repo root.")] = Path("."),
    lang: Annotated[
        str,
        typer.Option("--lang", help="Target: python (same-language) or c-rust (C->Rust rewrite)."),
    ] = "python",
    query: Annotated[
        str, typer.Option("--query", help="Worklist search query (contract predicates).")
    ] = "kind:pure covered:true",
    k: Annotated[int, typer.Option("--k", help="Candidates per component (cheap model).")] = 3,
    mode: Annotated[
        str, typer.Option("--mode", help="translate (source in context) or spec (contract only).")
    ] = "translate",
    c_source: Annotated[
        Path | None,
        typer.Option(
            "--c-source", help="c-rust: the compilable C translation unit (e.g. an amalgamation)."
        ),
    ] = None,
    c_flags: Annotated[
        list[str] | None,
        typer.Option("--c-flag", help="c-rust: a compile flag for the oracle build (repeatable)."),
    ] = None,
    n_trials: Annotated[
        int, typer.Option("--n-trials", help="c-rust: differential inputs per candidate.")
    ] = 300,
    pointers: Annotated[
        bool, typer.Option("--pointers", help="c-rust: include char*/byte-buffer pointer ABIs.")
    ] = False,
    out: Annotated[
        Path | None, typer.Option("--out", help="c-rust: write the full results JSON here.")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Cheap model override.")] = None,
    escalation_model: Annotated[
        str | None, typer.Option("--escalation-model", help="Escalation model override.")
    ] = None,
    budget_usd: Annotated[
        float | None, typer.Option("--budget-usd", help="Stop starting components past this.")
    ] = None,
    ledger: Annotated[
        Path | None,
        typer.Option("--ledger", help="Resumable ledger path (skips solved on rerun)."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="python: splice winners into the tree + final gate. "
            "c-rust: link the Rust in place of the C originals (--link-out).",
        ),
    ] = False,
    link_out: Annotated[
        Path | None,
        typer.Option("--link-out", help="c-rust --apply: dir for the patched C + Rust staticlib."),
    ] = None,
    force_link: Annotated[
        bool,
        typer.Option("--force-link", help="c-rust --apply: link even functions that read globals."),
    ] = False,
    no_tests: Annotated[
        bool,
        typer.Option(
            "--no-tests",
            help="Contract-only gating (measured ~6% false-pass rate — see experiment log).",
        ),
    ] = False,
    live: Annotated[
        bool,
        typer.Option("--live", help="Call the Anthropic API (requires cgir[llm] + API key)."),
    ] = False,
) -> None:
    """Rewrite every matching component through the sample->verify->escalate loop."""
    from cgir.report.impact import _is_test_spec
    from cgir.report.search import search_specs
    from cgir.rewrite import (
        DEFAULT_CHEAP_MODEL,
        DEFAULT_ESCALATION_MODEL,
        anthropic_sampler,
        rewrite_repo,
    )

    if lang == "c-rust":
        _rewrite_c_rust(
            index_dir,
            c_source,
            list(c_flags or []),
            k,
            n_trials,
            pointers,
            out,
            live,
            budget_usd,
            ledger,
            apply,
            link_out,
            force_link,
        )
        return
    if lang != "python":
        raise typer.BadParameter(f"--lang must be python or c-rust, got {lang!r}")

    specs = _load_specs(index_dir)
    worklist = [s for s in search_specs(specs, query, limit=None) if not _is_test_spec(s)]
    if not live:
        typer.echo(f"dry run: {len(worklist)} component(s) match {query!r}")
        for spec in sorted(worklist, key=lambda s: s.id):
            oracle = "contract+tests" if spec.covered_by else "contract-only"
            typer.echo(f"  {spec.id}  [{oracle}]")
        typer.echo(
            f"~{len(worklist) * k} cheap calls + escalations. "
            "Rerun with --live to generate (requires cgir[llm] + ANTHROPIC_API_KEY)."
        )
        return
    try:
        sampler = anthropic_sampler()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    report = rewrite_repo(
        index_dir,
        repo,
        sampler=sampler,
        query=query,
        k=k,
        mode=mode,
        cheap_model=model or DEFAULT_CHEAP_MODEL,
        escalation_model=escalation_model or DEFAULT_ESCALATION_MODEL,
        run_tests=not no_tests,
        budget_usd=budget_usd,
        ledger_path=ledger,
        apply=apply,
        log=typer.echo,
    )
    totals = report["totals"]
    typer.echo(
        f"solved {totals['solved']}/{totals['components']} "
        f"(unsolved {totals['unsolved']}, budget-stopped {totals['budget_exhausted']}) "
        f"for ${totals['cost_usd']}"
    )
    if apply:
        gate = report["final_gate"]
        typer.echo(
            f"applied {gate['applied']}; contract {'clean' if gate['contract_clean'] else 'DIRTY'}"
            + (
                ""
                if gate["tests_ok"] is None
                else f"; tests {'pass' if gate['tests_ok'] else 'FAIL'}"
            )
        )
        if not gate["contract_clean"] or gate["tests_ok"] is False:
            raise typer.Exit(code=1)


def _rewrite_c_rust(
    index_dir: Path,
    c_source: Path | None,
    c_flags: list[str],
    k: int,
    n_trials: int,
    pointers: bool,
    out: Path | None,
    live: bool,
    budget_usd: float | None,
    ledger: Path | None,
    apply: bool = False,
    link_out: Path | None = None,
    force_link: bool = False,
) -> None:
    import shutil

    from cgir.rewrite import anthropic_sampler
    from cgir.rewrite_c_rust import (
        c_rust_worklist,
        link_back,
        run_c_rust,
        suspect_global_reads,
    )

    if c_source is None:
        raise typer.BadParameter("--lang c-rust needs --c-source <amalgamation.c>")
    if not c_source.exists():
        raise typer.BadParameter(f"No such C source: {c_source}")
    if not live:
        entries, _ = c_rust_worklist(index_dir, c_source, pointers)
        typer.echo(f"dry run: {len(entries)} C leaf function(s) in {c_source.name} regenerable")
        for e in sorted(entries, key=lambda e: e.component_id):
            ptr = " [ptr]" if any(t.startswith("ptr:") for t, _ in e.params) else ""
            typer.echo(f"  {e.component_id}{ptr}")
        typer.echo(
            f"~{len(entries) * k} cheap calls + escalations, verified by differential vs the "
            "compiled C. Rerun with --live (requires cgir[llm], ANTHROPIC_API_KEY, cc + rustc)."
        )
        return
    for tool in ("cc", "rustc"):
        if shutil.which(tool) is None:
            raise typer.BadParameter(f"c-rust needs `{tool}` on PATH")
    try:
        sampler = anthropic_sampler()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    report = run_c_rust(
        index_dir,
        c_source,
        sampler=sampler,
        c_flags=c_flags,
        k=k,
        n_trials=n_trials,
        pointers=pointers,
        budget_usd=budget_usd,
        ledger_path=ledger,
        log=typer.echo,
    )
    if out is not None:
        out.write_text(json.dumps(report, indent=2) + "\n")
    totals = report["totals"]
    typer.echo(
        f"solved {totals['solved']}/{totals['components']} C->Rust "
        f"(unsolved {totals['unsolved']}) for ${totals['cost_usd']}; "
        f"stage kills: {report['stage_kills']}"
    )

    if not apply:
        return
    # Link the Rust in place of the C originals: the "C with Rust inside" step.
    by_id = {e.component_id: e for e in c_rust_worklist(index_dir, c_source, pointers)[0]}
    winners: dict[str, str] = {}
    skipped: list[str] = []
    for o in report["outcomes"]:
        if o["status"] != "solved":
            continue
        entry = by_id.get(o["component_id"])
        if entry is None:
            continue
        globals_read = suspect_global_reads(entry)
        if globals_read and not force_link:
            skipped.append(f"{entry.name} (reads {sorted(globals_read)})")
            continue
        winners[entry.name] = next(a["candidate"] for a in o["attempts"] if a["stage"] == "ok")
    if skipped:
        typer.echo(
            f"skipped {len(skipped)} state-reading function(s) (use --force-link): {skipped}"
        )
    if not winners:
        typer.echo("nothing safe to link.")
        return
    dest = link_out or (out.parent / "cgir-link" if out else Path("cgir-link"))
    gate = link_back(c_source, winners, dest, c_flags)
    if not gate["linked"]:
        typer.echo(f"link FAILED:\n{gate['error']}")
        raise typer.Exit(code=1)
    typer.echo(
        f"linked {len(gate['functions'])} Rust function(s) into {c_source.name}: "
        f"{gate['symbols_from_rust']} symbols provided by Rust, "
        f"{gate['c_definitions_renamed']} C definitions sidelined."
    )
    typer.echo(f"artifacts in {dest}/ (patched C, Rust staticlib, linked shared lib)")


@app.command(name="regenerate")
def regenerate_cmd(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    lang: Annotated[str, typer.Option("--lang")] = "typescript",
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    live: Annotated[
        bool,
        typer.Option("--live", help="Call the Anthropic API (requires cgir[llm] + API key)."),
    ] = False,
) -> None:
    """Print the prompt-pack for a component; --live generates real code."""
    spec_path = index_dir / "components" / f"{component_id}.json"
    if not spec_path.exists():
        raise typer.BadParameter(f"No spec at {spec_path}")
    spec = ComponentSpec.from_dict(json.loads(spec_path.read_text()))
    generator = None
    if live:
        from cgir.regenerate.regenerator import anthropic_generator

        try:
            generator = anthropic_generator()
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
    result = run_regenerate(spec, lang, generator=generator)
    typer.echo("--- PROMPT ---")
    typer.echo(result.prompt)
    typer.echo("--- OUTPUT (live) ---" if result.live else "--- OUTPUT (dry run) ---")
    typer.echo(result.code)


if __name__ == "__main__":  # pragma: no cover
    app()
