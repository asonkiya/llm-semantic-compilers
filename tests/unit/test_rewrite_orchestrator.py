"""RED-phase tests for the rewrite orchestrator (`cgir rewrite`).

The whole-repo loop over proven parts: worklist from a search query ->
pack prompt -> k cheap candidates -> contract verify (incremental) ->
component tests in a shadow -> one escalation -> budget cap -> resumable
ledger -> optional --apply with a final rescan+tests seatbelt.

Contract pinned here:

* ``rewrite_repo(index, repo, sampler=...)`` never touches the network —
  ``sampler: Callable[[prompt, model], tuple[str, float]]`` is the
  injectable seam (text, cost_usd), same convention as the regenerator.
* Candidates gate through verify: contract violations/hard drift kill a
  candidate; failing linked tests kill a candidate; first survivor wins.
* All-cheap-fail -> exactly one escalation attempt with failure feedback
  in the prompt; still failing -> "unsolved".
* ``budget_usd`` stops *starting* new components once spent.
* The ledger is written after every component and resuming skips solved
  components without sampling.
* ``apply=True`` splices winners into the working tree (descending span
  order within a file) and runs a final gate: rescan + contract diff.
"""

from __future__ import annotations

import json
from pathlib import Path

from cgir.pipeline import scan_repo
from cgir.rewrite import rewrite_repo

GOOD = "def add_tax(price, rate):\n    t = price * rate\n    return price + t\n"
EFFECTFUL = (
    "def add_tax(price, rate):\n"
    "    import requests\n"
    "    requests.post('http://x', json=price)\n"
    "    return price * (1 + rate)\n"
)
WRONG = "def add_tax(price, rate):\n    return 42.0\n"
GOOD_HELPER = "def double(x):\n    y = x + x\n    return y\n"


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "core.py").write_text(
        "def add_tax(price, rate):\n    return price * (1 + rate)\n\n\n"
        "def double(x):\n    return x * 2\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "from pkg.core import add_tax, double\n\n\n"
        "def test_add_tax():\n    assert abs(add_tax(100, 0.1) - 110) < 1e-6\n\n\n"
        "def test_double():\n    assert double(3) == 6\n"
    )
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    return repo, idx


def _sampler(script: dict[str, list[str]]):
    """Fake sampler: pops canned candidates per model; records calls."""
    calls: list[tuple[str, str]] = []

    def sample(prompt: str, model: str) -> tuple[str, float]:
        calls.append((prompt, model))
        return script[model].pop(0), 0.01

    sample.calls = calls  # type: ignore[attr-defined]
    return sample


def test_worklist_never_includes_test_components(tmp_path: Path) -> None:
    """Rewriting a test helper is rewriting the oracle — never on the list."""
    repo, idx = _repo(tmp_path)
    (repo / "tests" / "test_helpers.py").write_text(
        "def _fixture_price():\n    return 100\n\n\n"
        "def test_fixture():\n    assert _fixture_price() == 100\n"
    )
    from cgir.pipeline import scan_repo as rescan

    rescan(repo, out=idx)
    sampler = _sampler({"cheap": [GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=1
    )
    ids = {o["component_id"] for o in report["outcomes"]}
    assert not any(i.startswith("tests.") for i in ids)


def test_worklist_is_covered_pure_functions(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=1
    )
    ids = {o["component_id"] for o in report["outcomes"]}
    assert ids == {"pkg.core.add_tax", "pkg.core.double"}


def test_first_good_candidate_wins_no_escalation(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=3
    )
    by_id = {o["component_id"]: o for o in report["outcomes"]}
    assert by_id["pkg.core.add_tax"]["status"] == "solved"
    assert by_id["pkg.core.add_tax"]["solved_by"] == "cheap"
    assert all(m == "cheap" for _, m in sampler.calls)


def test_contract_kill_then_next_candidate(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [EFFECTFUL, GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=3
    )
    out = next(o for o in report["outcomes"] if o["component_id"] == "pkg.core.add_tax")
    assert out["status"] == "solved"
    assert len(out["attempts"]) == 2
    assert out["attempts"][0]["stage"] == "contract"


def test_wrong_behavior_killed_by_tests(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [WRONG, GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=3
    )
    out = next(o for o in report["outcomes"] if o["component_id"] == "pkg.core.add_tax")
    assert out["status"] == "solved"
    assert out["attempts"][0]["stage"] == "tests"


def test_escalation_rescue_and_feedback(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [WRONG, GOOD_HELPER], "esc": [GOOD]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=1
    )
    out = next(o for o in report["outcomes"] if o["component_id"] == "pkg.core.add_tax")
    assert out["status"] == "solved"
    assert out["solved_by"] == "escalation"
    esc_prompt = next(p for p, m in sampler.calls if m == "esc")
    assert "failed" in esc_prompt  # feedback carried into the escalation prompt


def test_unsolved_after_escalation(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [WRONG, GOOD_HELPER], "esc": [EFFECTFUL]})
    report = rewrite_repo(
        idx, repo, sampler=sampler, cheap_model="cheap", escalation_model="esc", k=1
    )
    out = next(o for o in report["outcomes"] if o["component_id"] == "pkg.core.add_tax")
    assert out["status"] == "unsolved"
    assert report["totals"]["unsolved"] == 1


def test_budget_stops_new_components(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx,
        repo,
        sampler=sampler,
        cheap_model="cheap",
        escalation_model="esc",
        k=1,
        budget_usd=0.005,  # first component's 0.01 exhausts it
    )
    statuses = [o["status"] for o in report["outcomes"]]
    assert "budget-exhausted" in statuses
    assert len(sampler.calls) == 1


def test_ledger_resume_skips_solved(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    ledger = tmp_path / "ledger.json"
    first = _sampler({"cheap": [GOOD, GOOD_HELPER]})
    rewrite_repo(
        idx,
        repo,
        sampler=first,
        cheap_model="cheap",
        escalation_model="esc",
        k=1,
        ledger_path=ledger,
    )
    assert json.loads(ledger.read_text())["totals"]["solved"] == 2
    second = _sampler({"cheap": []})
    report = rewrite_repo(
        idx,
        repo,
        sampler=second,
        cheap_model="cheap",
        escalation_model="esc",
        k=1,
        ledger_path=ledger,
    )
    assert not second.calls  # nothing re-sampled
    assert all(o["status"] == "solved" for o in report["outcomes"])


def test_apply_splices_winners_and_passes_final_gate(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    sampler = _sampler({"cheap": [GOOD, GOOD_HELPER]})
    report = rewrite_repo(
        idx,
        repo,
        sampler=sampler,
        cheap_model="cheap",
        escalation_model="esc",
        k=1,
        apply=True,
    )
    text = (repo / "pkg" / "core.py").read_text()
    # both winners (same file — descending-span splice must not corrupt)
    assert "t = price * rate" in text
    assert "y = x + x" in text
    assert report["final_gate"]["contract_clean"] is True
    assert report["final_gate"]["tests_ok"] is True
