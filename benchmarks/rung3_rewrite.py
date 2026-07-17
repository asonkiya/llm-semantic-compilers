"""Rung 3: the small-model rewrite benchmark (vision-rewrite.md).

The economic thesis made measurable: sample k candidates from a cheap model,
filter through deterministic contract verification + component tests, escalate
only failures to a bigger model. The headline number is *N% plug-in success at
$X per component*.

Two arms per component:

- ``spec``      — regeneration from the contract alone (pack without the target
                  source or covering-test bodies). Measures how much behavior
                  the contract + docstring actually pin down.
- ``translate`` — rewrite with the original source in context (restructure,
                  don't copy). The same-language proxy for rung 4's C->Rust
                  mechanics; expected near-ceiling, validates the pipeline.

Run with the *target repo's* interpreter (verify runs ``sys.executable -m
pytest`` in the shadow), with this package installed into it:

    uv pip install -e <cgir-repo> --python <target>/.venv/bin/python
    <target>/.venv/bin/python benchmarks/rung3_rewrite.py \
        --repo <target> --index <scan-out> --out results.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from cgir.cli import (
    _call_receivers,
    _component_source,
    _load_graph,
    _load_specs,
    _module_context,
    _type_sources,
)
from cgir.ir.nodes import NodeKind
from cgir.report.pack import build_pack, referenced_type_names, render_pack
from cgir.verify import verify

CHEAP_MODEL = "claude-haiku-4-5-20251001"
ESCALATION_MODEL = "claude-sonnet-4-6"
# USD per million tokens (input, output) — 2026-07 list prices.
PRICES = {CHEAP_MODEL: (1.00, 5.00), ESCALATION_MODEL: (3.00, 15.00)}

SYSTEM = (
    "You regenerate Python functions from their contracts. Output ONLY the "
    "complete function definition (starting with `def` or a decorator). "
    "No markdown fences, no prose, no module-level imports — if you need the "
    "stdlib, import inside the function body. Types named in the signature "
    "are already imported in the module."
)


@dataclass
class Attempt:
    model: str
    contract_ok: bool = False
    tests_ok: bool | None = None
    feedback: str = ""
    similarity: float | None = None


@dataclass
class ComponentResult:
    component_id: str
    arm: str
    solved_by: str | None = None  # "cheap" | "escalation" | None
    attempts: list[Attempt] = field(default_factory=list)
    verify_seconds: float = 0.0
    error: str = ""


def _pure_covered_functions(index: Path) -> list[str]:
    """Module-level pure functions with direct test coverage — the worklist."""
    graph = _load_graph(index)
    kind_by_qual = {
        n.attrs.get("qualname"): n.kind
        for n in graph.nodes()
        if n.kind in {NodeKind.Function, NodeKind.Method}
    }
    ids = []
    for spec in _load_specs(index):
        if spec.kind.value != "pure_function" or spec.id.startswith("tests."):
            continue
        if not spec.covered_by or set(spec.effects) - {"raise"}:
            continue
        if kind_by_qual.get(spec.id) is not NodeKind.Function:
            continue
        ids.append(spec.id)
    return sorted(ids)


def _prompt(index: Path, repo: Path, component_id: str, arm: str) -> str:
    specs = _load_specs(index)
    target = next(s for s in specs if s.id == component_id)
    graph = _load_graph(index)
    source = _component_source(graph, component_id, repo)
    bundle = build_pack(
        specs,
        component_id,
        source=source if arm == "translate" else None,
        types=_type_sources(graph, referenced_type_names(target), repo),
        tests={},  # never leak the oracle
        context=_module_context(graph, component_id, repo),
        receivers=_call_receivers(graph, target),
    )
    pack_text = render_pack(bundle)
    name = component_id.rsplit(".", 1)[-1]
    if arm == "spec":
        task = (
            f"Implement `{name}` from this contract. Copy the signature "
            "verbatim (including annotations). The function must be pure: no "
            "I/O, no globals, no argument mutation. The docstring describes "
            "the behavior — implement it faithfully, including edge cases it "
            "implies."
        )
    else:
        task = (
            f"Rewrite `{name}` (original included above). Preserve exact "
            "behavior and the verbatim signature, but restructure the "
            "implementation — different control flow or decomposition, your "
            "own local names. Do not copy the original line-for-line."
        )
    return f"{pack_text}\n\n---\n\n{task}"


def _extract(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    # drop any prose before the first def/decorator
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("def ", "@", "async def ")):
            return "\n".join(lines[i:])
    return text


def _normalized(src: str) -> str:
    out = []
    for line in src.splitlines():
        line = re.sub(r"#.*", "", line).strip()
        if line:
            out.append(re.sub(r"\s+", " ", line))
    return "\n".join(out)


class Ledger:
    def __init__(self) -> None:
        self.tokens: dict[str, list[int]] = {m: [0, 0] for m in PRICES}

    def add(self, model: str, usage: Any) -> None:
        self.tokens[model][0] += usage.input_tokens
        self.tokens[model][1] += usage.output_tokens

    def cost(self, model: str | None = None) -> float:
        models = [model] if model else list(PRICES)
        return sum(
            self.tokens[m][0] * PRICES[m][0] / 1e6 + self.tokens[m][1] * PRICES[m][1] / 1e6
            for m in models
        )


def _generate(
    client: anthropic.Anthropic, model: str, prompt: str, k: int, ledger: Ledger
) -> list[str]:
    def one(_: int) -> str:
        msg = client.messages.create(
            model=model,
            max_tokens=3000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        ledger.add(model, msg.usage)
        return _extract(msg.content[0].text)

    with ThreadPoolExecutor(max_workers=k) as pool:
        return list(pool.map(one, range(k)))


def _check(
    index: Path, repo: Path, component_id: str, candidate: str, result: ComponentResult
) -> tuple[bool, bool, str]:
    """Contract-check then test-check. Returns (contract_ok, tests_ok, feedback)."""
    t0 = time.monotonic()
    try:
        contract = verify(index, component_id, candidate, repo, run_tests=False)
    except Exception as exc:  # syntax errors etc. — count as a contract failure
        result.verify_seconds += time.monotonic() - t0
        return False, False, f"verify error: {exc}"
    if not contract.contract_ok:
        result.verify_seconds += time.monotonic() - t0
        return False, False, f"contract drift: {contract.drift} violations: {contract.violations}"
    tested = verify(index, component_id, candidate, repo, run_tests=True)
    result.verify_seconds += time.monotonic() - t0
    if tested.tests_ok is False:
        return True, False, f"tests failed:\n{tested.detail}"
    return True, True, ""


def run_component(
    client: anthropic.Anthropic,
    index: Path,
    repo: Path,
    component_id: str,
    arm: str,
    k: int,
    ledger: Ledger,
    original: str,
) -> ComponentResult:
    result = ComponentResult(component_id=component_id, arm=arm)
    prompt = _prompt(index, repo, component_id, arm)

    for model, tier, n in ((CHEAP_MODEL, "cheap", k), (ESCALATION_MODEL, "escalation", 1)):
        if tier == "escalation":
            best = max(result.attempts, key=lambda a: a.contract_ok, default=None)
            if best is None or not best.feedback:
                break
            prompt = (
                f"{prompt}\n\nA previous attempt failed verification. "
                f"Feedback:\n{best.feedback}\n\nProduce a corrected implementation."
            )
        for candidate in _generate(client, model, prompt, n, ledger):
            attempt = Attempt(model=model)
            if arm == "translate":
                attempt.similarity = difflib.SequenceMatcher(
                    None, _normalized(original), _normalized(candidate)
                ).ratio()
            attempt.contract_ok, ok, attempt.feedback = _check(
                index, repo, component_id, candidate, result
            )
            attempt.tests_ok = ok
            result.attempts.append(attempt)
            if ok:
                result.solved_by = tier
                return result
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--arm", choices=["spec", "translate", "both"], default="both")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    client = anthropic.Anthropic()
    ledger = Ledger()
    ids = args.only or _pure_covered_functions(args.index)
    if args.limit:
        ids = ids[: args.limit]
    arms = ["spec", "translate"] if args.arm == "both" else [args.arm]
    graph = _load_graph(args.index)

    results: list[ComponentResult] = []
    for cid in ids:
        original = _component_source(graph, cid, args.repo) or ""
        for arm in arms:
            t0 = time.monotonic()
            try:
                res = run_component(
                    client, args.index, args.repo, cid, arm, args.k, ledger, original
                )
            except Exception as exc:
                res = ComponentResult(component_id=cid, arm=arm, error=str(exc))
            results.append(res)
            status = res.solved_by or ("ERROR: " + res.error if res.error else "unsolved")
            print(
                f"[{arm:9s}] {cid:60s} {status:12s} "
                f"attempts={len(res.attempts)} {time.monotonic() - t0:5.1f}s "
                f"${ledger.cost():.3f} cum",
                flush=True,
            )

    report = {
        "repo": str(args.repo),
        "k": args.k,
        "models": {"cheap": CHEAP_MODEL, "escalation": ESCALATION_MODEL},
        "components": len(ids),
        "arms": {},
        "cost_usd": {
            "cheap": round(ledger.cost(CHEAP_MODEL), 4),
            "escalation": round(ledger.cost(ESCALATION_MODEL), 4),
            "total": round(ledger.cost(), 4),
        },
        "tokens": ledger.tokens,
        "results": [asdict(r) for r in results],
    }
    for arm in arms:
        rs = [r for r in results if r.arm == arm and not r.error]
        solved_cheap = sum(r.solved_by == "cheap" for r in rs)
        solved_esc = sum(r.solved_by == "escalation" for r in rs)
        sims = [
            a.similarity for r in rs for a in r.attempts if a.similarity is not None and a.tests_ok
        ]
        report["arms"][arm] = {
            "n": len(rs),
            "solved_cheap": solved_cheap,
            "solved_escalation": solved_esc,
            "unsolved": len(rs) - solved_cheap - solved_esc,
            "plug_in_rate": round((solved_cheap + solved_esc) / len(rs), 3) if rs else None,
            "cheap_only_rate": round(solved_cheap / len(rs), 3) if rs else None,
            "mean_passing_similarity": round(sum(sims) / len(sims), 3) if sims else None,
        }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
