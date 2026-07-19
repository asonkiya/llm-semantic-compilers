"""The rewrite orchestrator — the whole-repo loop over proven parts.

    worklist (search query over the index)
      -> pack prompt (contract + types + module context; source in
         translate mode)
      -> k candidates from the cheap model
      -> contract verify (incremental)  ->  linked tests in a shadow
      -> one escalation carrying the failure feedback
      -> resumable ledger, budget cap, optional --apply with a final
         rescan + contract-diff + full-test seatbelt

The generation seam is an injectable ``sampler: Callable[[prompt, model],
(text, cost_usd)]`` so the loop is testable offline — only
:func:`anthropic_sampler` touches the network, and only when the CLI is
run with ``--live``. Empirical grounding for every stage is in
docs/experiment-log.md (rungs 3-4): tests are the oracle where they
exist; the contract stage is a cheap pre-filter, not a semantic judge;
escalation feedback measurably rescues near-misses.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cgir.export.json_export import read_specs
from cgir.ir.component_spec import ComponentSpec
from cgir.report.impact import _is_test_spec
from cgir.report.search import search_specs
from cgir.verify import _find_node, _hard_drift, _splice, verify

Sampler = Callable[[str, str], tuple[str, float]]
# Behavioral oracle seam: given (component_id, candidate), return
# (passed, feedback). Injected to swap the default pytest oracle for a
# differential or capture/replay one — the C->Rust harness plugs its
# differential-vs-original here. Feedback flows into the escalation prompt.
BehavioralOracle = Callable[[str, str], tuple[bool, str]]

DEFAULT_QUERY = "kind:pure covered:true"
DEFAULT_CHEAP_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_ESCALATION_MODEL = "claude-sonnet-4-6"
# USD per million tokens (input, output).
PRICES = {
    DEFAULT_CHEAP_MODEL: (1.00, 5.00),
    DEFAULT_ESCALATION_MODEL: (3.00, 15.00),
}

_SYSTEM = (
    "You rewrite one component of a codebase against its contract. Output "
    "ONLY the complete function definition — no markdown fences, no prose, "
    "no module-level imports (import inside the function body if needed). "
    "Types named in the signature are already in scope in the module."
)


@dataclass(slots=True)
class RewriteAttempt:
    tier: str  # cheap | escalation
    model: str
    candidate: str
    stage: str = ""  # contract | tests | ok
    feedback: str = ""


@dataclass(slots=True)
class ComponentOutcome:
    component_id: str
    status: str = "pending"  # solved | unsolved | budget-exhausted
    solved_by: str | None = None
    oracle: str = "contract+tests"  # contract-only when no linked tests ran
    attempts: list[RewriteAttempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_code(text: str) -> str:
    m = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("def ", "@", "async def ", "#[", "pub ", "fn ")):
            return "\n".join(lines[i:])
    return text


def _build_prompt(index_dir: Path, repo: Path, spec: ComponentSpec, mode: str) -> str:
    # Lazy import: the CLI lazily imports this module for the command, and
    # these pack-assembly helpers live beside the CLI today.
    from cgir.cli import (
        _call_receivers,
        _component_source,
        _load_graph,
        _module_context,
        _type_sources,
    )
    from cgir.report.pack import build_pack, referenced_type_names, render_pack

    specs = read_specs(index_dir)
    graph = _load_graph(index_dir)
    source = _component_source(graph, spec.id, repo)
    bundle = build_pack(
        specs,
        spec.id,
        source=source if mode == "translate" else None,
        types=_type_sources(graph, referenced_type_names(spec), repo),
        tests={},  # the oracle never leaks into the prompt
        context=_module_context(graph, spec.id, repo),
        receivers=_call_receivers(graph, spec),
    )
    name = spec.id.rsplit(".", 1)[-1]
    if mode == "spec":
        task = (
            f"Implement `{name}` from this contract. Copy the signature verbatim. "
            "Pure: no I/O, no globals, no argument mutation. Implement the "
            "docstring faithfully, including the edge cases it implies."
        )
    else:
        task = (
            f"Rewrite `{name}` (original included above). Preserve exact behavior "
            "and the verbatim signature; restructure the implementation."
        )
    return f"{render_pack(bundle)}\n\n---\n\n{task}"


def _check(
    index_dir: Path,
    repo: Path,
    component_id: str,
    candidate: str,
    run_tests: bool,
    oracle: BehavioralOracle | None,
) -> tuple[str, str, bool]:
    """Returns (stage, feedback, behavioral_ran). stage == "ok" means accepted.

    Stage 1 is always the cgir contract check. Stage 2 is the behavioral
    oracle: the injected ``oracle`` if given (differential, capture/replay,
    ...), else the default pytest-in-a-shadow path. ``run_tests=False`` with
    no oracle is contract-only gating (measured ~6% false-pass — a
    pre-filter, not a judge)."""
    try:
        contract = verify(index_dir, component_id, candidate, repo, run_tests=False)
    except Exception as exc:
        return "contract", f"verify error: {exc}", False
    if not contract.contract_ok or contract.violations:
        fb = f"contract drift: {contract.drift} violations: {contract.violations}"
        return "contract", fb, False
    if oracle is not None:
        ok, feedback = oracle(component_id, candidate)
        return ("ok", "", True) if ok else ("behavioral", feedback, True)
    if not run_tests:
        return "ok", "", False
    tested = verify(index_dir, component_id, candidate, repo, run_tests=True)
    if tested.tests_ok is False:
        return "tests", f"tests failed:\n{tested.detail}", True
    return "ok", "", bool(tested.tests_ran)


def rewrite_repo(
    index_dir: Path,
    repo: Path,
    *,
    sampler: Sampler,
    query: str = DEFAULT_QUERY,
    k: int = 3,
    mode: str = "translate",
    cheap_model: str = DEFAULT_CHEAP_MODEL,
    escalation_model: str = DEFAULT_ESCALATION_MODEL,
    run_tests: bool = True,
    oracle: BehavioralOracle | None = None,
    budget_usd: float | None = None,
    ledger_path: Path | None = None,
    apply: bool = False,
    log: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Run the rewrite loop over every component matching ``query``.

    ``oracle`` swaps the default pytest behavioral check for an injected one
    (differential, capture/replay) — the seam that lets one orchestrator
    drive both the Python-with-tests and the C->Rust-with-differential
    pipelines over the same worklist/escalation/ledger/budget machinery."""
    specs = read_specs(index_dir)
    # Test components are the oracle; the loop must never rewrite them.
    worklist = [s for s in search_specs(specs, query, limit=None) if not _is_test_spec(s)]
    prior: dict[str, dict[str, Any]] = {}
    if ledger_path is not None and ledger_path.exists():
        prior = {
            o["component_id"]: o
            for o in json.loads(ledger_path.read_text()).get("outcomes", [])
            if o["status"] == "solved"
        }

    outcomes: list[ComponentOutcome | dict[str, Any]] = []
    spent = 0.0

    def _flush() -> dict[str, Any]:
        dicts = [o if isinstance(o, dict) else o.to_dict() for o in outcomes]
        solved = sum(o["status"] == "solved" for o in dicts)
        report = {
            "query": query,
            "mode": mode,
            "k": k,
            "models": {"cheap": cheap_model, "escalation": escalation_model},
            "totals": {
                "components": len(worklist),
                "solved": solved,
                "unsolved": sum(o["status"] == "unsolved" for o in dicts),
                "budget_exhausted": sum(o["status"] == "budget-exhausted" for o in dicts),
                "cost_usd": round(spent, 4),
            },
            "outcomes": dicts,
        }
        if ledger_path is not None:
            ledger_path.write_text(json.dumps(report, indent=2) + "\n")
        return report

    for spec in sorted(worklist, key=lambda s: s.id):
        if spec.id in prior:
            outcomes.append(prior[spec.id])
            continue
        out = ComponentOutcome(component_id=spec.id)
        outcomes.append(out)
        if budget_usd is not None and spent >= budget_usd:
            out.status = "budget-exhausted"
            _flush()
            continue
        prompt = _build_prompt(index_dir, repo, spec, mode)
        for model, tier, n in ((cheap_model, "cheap", k), (escalation_model, "escalation", 1)):
            if tier == "escalation":
                fb = next((a.feedback for a in reversed(out.attempts) if a.feedback), "")
                if not fb:
                    break
                prompt = (
                    f"{prompt}\n\nA previous attempt failed verification. Feedback:\n{fb}\n\n"
                    "Produce a corrected implementation."
                )
            for _ in range(n):
                text, cost = sampler(prompt, model)
                spent += cost
                candidate = _extract_code(text)
                attempt = RewriteAttempt(tier=tier, model=model, candidate=candidate)
                out.attempts.append(attempt)
                attempt.stage, attempt.feedback, behavioral_ran = _check(
                    index_dir, repo, spec.id, candidate, run_tests, oracle
                )
                if attempt.stage == "ok":
                    out.status = "solved"
                    out.solved_by = tier
                    if not behavioral_ran:
                        out.oracle = "contract-only"
                    elif oracle is not None:
                        out.oracle = "contract+behavioral"
                    else:
                        out.oracle = "contract+tests"
                    break
            if out.status == "solved":
                break
        if out.status == "pending":
            out.status = "unsolved"
        log(f"{spec.id}: {out.status} ({len(out.attempts)} attempts, ${spent:.3f} spent)")
        _flush()

    report = _flush()
    if apply:
        report["final_gate"] = _apply_winners(index_dir, repo, report, run_tests=run_tests)
        if ledger_path is not None:
            ledger_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def _apply_winners(
    index_dir: Path, repo: Path, report: dict[str, Any], run_tests: bool
) -> dict[str, Any]:
    """Splice winners into the working tree, then run the final seatbelt:
    rescan -> no hard contract drift anywhere -> full test run.

    Splices within a file go in descending span order so earlier splices
    don't shift later spans.
    """
    winners: list[tuple[Any, str]] = []
    for o in report["outcomes"]:
        if o["status"] != "solved":
            continue
        node = _find_node(index_dir, o["component_id"])
        if node is None or node.path is None:
            continue
        winners.append((node, o["attempts"][-1]["candidate"]))
    winners.sort(key=lambda t: (t[0].path, -(t[0].start_line or 0)))
    for node, candidate in winners:
        assert node.start_line is not None and node.end_line is not None
        _splice(repo / node.path, node.start_line, node.end_line, candidate)

    import tempfile

    from cgir.pipeline import scan_repo
    from cgir.report.diff import compute_diff

    new_index = Path(tempfile.mkdtemp(prefix="cgir-rewrite-gate-")) / "idx"
    scan_repo(repo, out=new_index)
    diff = compute_diff(read_specs(index_dir), read_specs(new_index))
    dirty = [c["id"] for c in diff["changed"] if _hard_drift(c)]

    tests_ok: bool | None = None
    if run_tests:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        tests_ok = proc.returncode == 0
    return {
        "applied": len(winners),
        "contract_clean": not dirty,
        "hard_drift": dirty,
        "tests_ok": tests_ok,
    }


def anthropic_sampler(max_tokens: int = 3000) -> Sampler:
    """The live sampler (``pip install cgir[llm]``, ``ANTHROPIC_API_KEY``)."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised via CLI
        raise RuntimeError(
            "Install cgir[llm] to run live rewrites (adds the anthropic package)"
        ) from exc

    client = anthropic.Anthropic()

    def sample(prompt: str, model: str) -> tuple[str, float]:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        rates = PRICES.get(model, (3.00, 15.00))
        cost = msg.usage.input_tokens * rates[0] / 1e6 + msg.usage.output_tokens * rates[1] / 1e6
        return "".join(b.text for b in msg.content if b.type == "text"), cost

    return sample
