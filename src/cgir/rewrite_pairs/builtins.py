"""Builtin rewrite pairs — the proven engines exposed through the registry.

``run_c_rust`` and ``run_python_rust`` are wrapped as :class:`FunctionPair`s so
they become discoverable/uniform without touching their verified code. Their
engine modules are imported lazily (inside ``call``) so importing the registry
stays cheap — a plain ``cgir rewrite --help`` or a discovery test doesn't pull
in rustc/anthropic machinery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cgir.rewrite import Sampler
from cgir.rewrite_pairs.base import FunctionPair


def _run_c_rust(
    index_dir: Path,
    source: Path,
    *,
    sampler: Sampler,
    opts: dict[str, Any],
    k: int,
    budget_usd: float | None,
    ledger_path: Path | None,
    log: Any,
) -> dict[str, Any]:
    from cgir.rewrite_c_rust import run_c_rust

    return run_c_rust(
        index_dir,
        source,
        sampler=sampler,
        c_flags=opts.get("c_flags"),
        k=k,
        n_trials=opts.get("n_trials", 300),
        pointers=opts.get("pointers", False),
        include_nonleaf=opts.get("include_nonleaf", False),
        structs=opts.get("structs", False),
        budget_usd=budget_usd,
        ledger_path=ledger_path,
        log=log,
    )


def _run_python_rust(
    index_dir: Path,
    source: Path,
    *,
    sampler: Sampler,
    opts: dict[str, Any],
    k: int,
    budget_usd: float | None,
    ledger_path: Path | None,
    log: Any,
) -> dict[str, Any]:
    from cgir.rewrite_python_rust import run_python_rust

    return run_python_rust(
        index_dir,
        source,
        sampler=sampler,
        traces=opts["traces"],
        query=opts.get("query", "kind:pure covered:true"),
        k=k,
        min_traces=opts.get("min_traces", 3),
        budget_usd=budget_usd,
        ledger_path=ledger_path,
        log=log,
    )


BUILTIN_PAIRS = (
    FunctionPair(
        name="c-rust",
        description="C → Rust, verified by ABI differential fuzzing + a whole-program gate.",
        call=_run_c_rust,
    ),
    FunctionPair(
        name="python-rust",
        description="Python → Rust, verified by replaying recorded (args, result) traces.",
        call=_run_python_rust,
    ),
)
