"""RewritePair — the per-(source→target) seam over one shared rewrite engine.

The verified-rewrite core (:func:`cgir.rewrite.run_search_loop`) is already
language-neutral: k cheap candidates per item → ``evaluate`` (first "ok" wins)
→ one escalation with feedback → resumable ledger + budget cap. A *rewrite
pair* supplies only what differs between C→Rust, Python→Rust, Python→Python…:

* **worklist** — eligibility: index + source → items to rewrite (+ exclusions),
* **build_prompt** — the model instruction for one item,
* **evaluate** — compile the candidate and run the behavioral oracle,
* **apply** (optional) — splice the winners back into the project.

Everything else (the search loop, escalation, ledger, budget, reporting) is
inherited from :class:`SearchLoopPair`. Adding a pair is one small class, not a
re-implemented pipeline — exactly the deal :mod:`cgir.languages` gives the
analysis layer. See ``docs/design-rewrite-pairs.md``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cgir.rewrite import Sampler, run_search_loop

# Bump when the RewritePair surface changes incompatibly. Mirrors
# cgir.languages.base.ADAPTER_API_VERSION: a plugin declaring an older version
# still loads (with a warning) since new methods get base-class defaults.
PAIR_API_VERSION = 1


@dataclass
class PairContext:
    """Per-run state a pair builds once in :meth:`SearchLoopPair.prepare` and
    threads through ``build_prompt``/``evaluate`` — e.g. the compiled oracle,
    compiler-probed constants, a scratch ``workdir``. ``extra`` is pair-owned."""

    workdir: Path
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RewritePair(Protocol):
    """The registry contract. Every pair — builtin or ``pip``-installed plugin —
    exposes ``name``/``description`` and a ``run`` that returns the standard
    report dict (``totals``, ``outcomes``, …). :class:`SearchLoopPair` provides
    ``run`` from the fine-grained methods; builtins may wrap an existing engine."""

    name: str
    description: str

    def run(
        self,
        index_dir: Path,
        source: Path,
        *,
        sampler: Sampler,
        opts: dict[str, Any] | None = ...,
        k: int = ...,
        budget_usd: float | None = ...,
        ledger_path: Path | None = ...,
        log: Any = ...,
    ) -> dict[str, Any]: ...


class SearchLoopPair:
    """Base for a pair built on the shared search loop. Subclass and implement
    ``worklist``/``build_prompt``/``evaluate`` (and optionally ``prepare``,
    ``id_of``, ``apply``); ``run`` wires them into
    :func:`cgir.rewrite.run_search_loop`."""

    name: str = "unnamed-pair"
    description: str = ""
    api_version: int = PAIR_API_VERSION

    # --- the seam a subclass fills ------------------------------------------
    def worklist(
        self, index_dir: Path, source: Path, opts: dict[str, Any]
    ) -> tuple[list[Any], list[tuple[str, str]]]:
        """(items, excluded[(id, reason)]) — the eligible worklist."""
        raise NotImplementedError

    def prepare(self, entries: list[Any], source: Path, opts: dict[str, Any]) -> PairContext:
        """Build once-per-run state (oracle, probe, workdir). Default: a scratch
        ``workdir`` and no extra state."""
        return PairContext(workdir=Path(tempfile.mkdtemp(prefix=f"cgir-{self.name}-")))

    def build_prompt(self, entry: Any, ctx: PairContext) -> str:
        raise NotImplementedError

    def evaluate(
        self, entry: Any, candidate: str, ctx: PairContext
    ) -> tuple[str, str, dict[str, Any]]:
        """Return ``(stage, feedback, meta)``; ``stage == "ok"`` accepts the
        candidate and merges ``meta`` into its outcome."""
        raise NotImplementedError

    def id_of(self, entry: Any) -> str:
        cid: str = entry.component_id
        return cid

    def apply(
        self, winners: dict[str, Any], ctx: PairContext, opts: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError(f"pair {self.name!r} does not support --apply")

    # --- inherited machinery -------------------------------------------------
    def run(
        self,
        index_dir: Path,
        source: Path,
        *,
        sampler: Sampler,
        opts: dict[str, Any] | None = None,
        k: int = 3,
        budget_usd: float | None = None,
        ledger_path: Path | None = None,
        log: Any = lambda _: None,
    ) -> dict[str, Any]:
        opts = opts or {}
        entries, excluded = self.worklist(index_dir, source, opts)
        ctx = self.prepare(entries, source, opts)
        report = run_search_loop(
            entries,
            build_prompt=lambda e: self.build_prompt(e, ctx),
            evaluate=lambda e, cand: self.evaluate(e, cand, ctx),
            sampler=sampler,
            id_of=self.id_of,
            k=k,
            budget_usd=budget_usd,
            ledger_path=ledger_path,
            report_meta={"pair": self.name, "source": str(source)},
            log=log,
        )
        report["excluded"] = excluded
        report["excluded_count"] = len(excluded)
        report["_ctx"] = ctx  # so a caller can --apply without re-preparing
        return report


@dataclass
class FunctionPair:
    """Adapts an existing engine function (``run_c_rust``/``run_python_rust``)
    to the registry without rewriting it — the proven pipelines become
    discoverable/uniform pairs while their code is untouched. ``call`` maps the
    uniform ``run`` kwargs onto the engine's own signature."""

    name: str
    description: str
    call: Any  # (index_dir, source, *, sampler, opts, k, budget_usd, ledger_path, log) -> report

    def run(
        self,
        index_dir: Path,
        source: Path,
        *,
        sampler: Sampler,
        opts: dict[str, Any] | None = None,
        k: int = 3,
        budget_usd: float | None = None,
        ledger_path: Path | None = None,
        log: Any = lambda _: None,
    ) -> dict[str, Any]:
        report: dict[str, Any] = self.call(
            index_dir,
            source,
            sampler=sampler,
            opts=opts or {},
            k=k,
            budget_usd=budget_usd,
            ledger_path=ledger_path,
            log=log,
        )
        return report
