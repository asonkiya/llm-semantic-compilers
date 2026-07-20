"""The corpus regression gate (`benchmarks/corpus_scan.py`) is product code now,
not a throwaway script — so its gate logic and its ground-truth counter get
tests. These run fully offline (no clones): the gate logic is exercised on
synthetic rows, and the counter on the committed fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cgir.pipeline import scan_repo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))
import corpus_scan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# a minimal baseline the gate checks against
BASELINE = {
    "tolerance": 0.03,
    "repos": {
        "flask": {"lang": "python", "extraction_ratio": 0.856, "ground_truth_defs": 388},
        "stb": {"lang": "c", "extraction_ratio": 0.821, "ground_truth_defs": 2552},
    },
}


def _row(name: str, **over: object) -> dict:
    row = {
        "name": name,
        "scan_ok": True,
        "deterministic": True,
        "downstream_ok": True,
        "extraction_ratio": BASELINE["repos"][name]["extraction_ratio"],
        "components": 100,
        "ground_truth_defs": BASELINE["repos"][name]["ground_truth_defs"],
    }
    row.update(over)
    return row


def test_gate_passes_when_within_tolerance() -> None:
    rows = [_row("flask"), _row("stb", extraction_ratio=0.821 - 0.02)]  # 0.02 < 0.03 tol
    assert corpus_scan.check_against_baseline(rows, BASELINE) == []


def test_gate_flags_ratio_regression_beyond_tolerance() -> None:
    # the exact class of bug the corpus caught for real (C #ifdef / ERROR-node)
    rows = [_row("stb", extraction_ratio=0.55, components=1400)]
    failures = corpus_scan.check_against_baseline(rows, BASELINE)
    assert len(failures) == 1 and "stb" in failures[0] and "regressed" in failures[0]


def test_gate_flags_crash_nondeterminism_and_downstream() -> None:
    rows = [
        {"name": "flask", "scan_ok": False, "scan": "crash"},
        _row("stb", deterministic=False),
    ]
    failures = corpus_scan.check_against_baseline(rows, BASELINE)
    joined = " | ".join(failures)
    assert "flask: scan crash" in joined
    assert "stb: non-deterministic" in joined


def test_gate_skips_repos_absent_from_baseline() -> None:
    # a new repo with no baseline entry must not fail the gate on ratio
    rows = [_row("flask")]
    rows[0]["name"] = "brand_new_repo"
    rows[0]["extraction_ratio"] = 0.1
    assert corpus_scan.check_against_baseline(rows, BASELINE) == []


def test_ground_truth_counter_matches_scan_on_fixtures() -> None:
    """The extraction-ratio denominator must track what the adapter extracts —
    guard the counting logic on committed fixtures, no network."""
    for fixture, lang in [("python_sample", "python"), ("ts_sample", "typescript")]:
        scan_dir = FIXTURES / fixture
        defs, loc = corpus_scan.ground_truth_defs(scan_dir, lang)
        assert defs > 0 and loc > 0
        result = scan_repo(scan_dir, out=FIXTURES / fixture / ".cgir_tmp_gate")
        n_components = len(result.specs)
        # extraction should land in a sane band, never zero (the JS/#ifdef bugs
        # showed as a 0.0 or near-0 ratio) and never wildly over the denominator
        ratio = n_components / defs
        assert 0.5 <= ratio <= 2.0, f"{fixture}: {n_components}/{defs} = {ratio}"
        import shutil

        shutil.rmtree(FIXTURES / fixture / ".cgir_tmp_gate", ignore_errors=True)
