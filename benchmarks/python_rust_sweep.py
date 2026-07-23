"""Python->Rust addressable-surface sweep — how much of real Python code is
eligible for the ``cgir rewrite --lang python-rust`` pipeline, and why the rest
isn't.

For a spread of real public Python libraries, scan the package and run
``python_rust_worklist`` over every *pure* function (``kind:pure`` — coverage is
a separate axis, needed only to *capture* traces, not to decide eligibility).
Report the eligible count, a histogram of *why* pure functions are rejected,
and the return-kind mix of the eligibles. This both maps the v1 addressable
surface (scalar + str/bytes leaves) and stress-tests the ``ast``-based
eligibility parser on real-world annotations — it is how the ``async def``
misclassification was found.

    python benchmarks/python_rust_sweep.py --out python-rust-sweep.json
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import traceback
from pathlib import Path

from cgir.ffi.sources.python import python_rust_worklist
from cgir.pipeline import scan_repo

# name, git url, package subdir (".": whole repo). A spread of styles: web
# frameworks, heavily-typed libs (pydantic, attrs, mypy, black), functional
# libs (toolz, more-itertools), and string/parse utilities.
CORPUS: list[tuple[str, str, str]] = [
    ("flask", "https://github.com/pallets/flask", "src"),
    ("requests", "https://github.com/psf/requests", "src"),
    ("click", "https://github.com/pallets/click", "src"),
    ("httpx", "https://github.com/encode/httpx", "httpx"),
    ("rich", "https://github.com/Textualize/rich", "rich"),
    ("sqlalchemy", "https://github.com/sqlalchemy/sqlalchemy", "lib"),
    ("pydantic", "https://github.com/pydantic/pydantic", "pydantic"),
    ("django", "https://github.com/django/django", "django"),
    ("attrs", "https://github.com/python-attrs/attrs", "src"),
    ("more-itertools", "https://github.com/more-itertools/more-itertools", "."),
    ("sortedcontainers", "https://github.com/grantjenks/python-sortedcontainers", "src"),
    ("packaging", "https://github.com/pypa/packaging", "src"),
    ("sqlparse", "https://github.com/andialbrecht/sqlparse", "."),
    ("jsonschema", "https://github.com/python-jsonschema/jsonschema", "."),
    ("boltons", "https://github.com/mahmoud/boltons", "."),
    ("toolz", "https://github.com/pytoolz/toolz", "."),
    ("pygments", "https://github.com/pygments/pygments", "."),
    ("jinja2", "https://github.com/pallets/jinja", "src"),
    ("werkzeug", "https://github.com/pallets/werkzeug", "src"),
    ("urllib3", "https://github.com/urllib3/urllib3", "src"),
    ("tabulate", "https://github.com/astanin/python-tabulate", "."),
    ("markupsafe", "https://github.com/pallets/markupsafe", "src"),
    ("cachetools", "https://github.com/tkem/cachetools", "src"),
    ("mypy", "https://github.com/python/mypy", "mypy"),
    ("black", "https://github.com/psf/black", "src"),
    ("semver", "https://github.com/python-semver/python-semver", "src"),
]


def clone(name: str, url: str, cache: Path) -> Path | None:
    dest = cache / name
    if dest.exists():
        return dest
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return dest if proc.returncode == 0 else None


def _reason_bucket(reason: str) -> str:
    return reason.split("(")[0].split("`")[0].strip()[:42]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("benchmarks/python-rust-sweep.json"))
    ap.add_argument("--cache", type=Path, default=Path("/tmp/cgir-pyrust-corpus"))
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    work = args.cache / "_indexes"
    work.mkdir(exist_ok=True)
    picks = set(args.only.split(",")) if args.only else None

    reasons: collections.Counter[str] = collections.Counter()
    ret_kinds: collections.Counter[str] = collections.Counter()
    rows: list[dict] = []
    for name, url, subdir in CORPUS:
        if picks and name not in picks:
            continue
        print(f"[{name:16s}]", end=" ", flush=True)
        repo = clone(name, url, args.cache)
        if repo is None:
            print("CLONE FAIL")
            rows.append({"name": name, "eligible": -1, "pure": 0, "error": "clone"})
            continue
        scan_dir = repo / subdir if subdir != "." and (repo / subdir).exists() else repo
        try:
            scan_repo(scan_dir, out=work / name)
            entries, excluded = python_rust_worklist(work / name, scan_dir, query="kind:pure")
        except Exception:
            print("CRASH")
            rows.append(
                {"name": name, "eligible": -2, "pure": 0, "error": traceback.format_exc()[-600:]}
            )
            continue
        for _cid, reason in excluded:
            reasons[_reason_bucket(reason)] += 1
        for e in entries:
            ret_kinds[e.sig.ret] += 1
        pure = len(entries) + len(excluded)
        rows.append(
            {
                "name": name,
                "eligible": len(entries),
                "pure": pure,
                "sample": [e.symbol for e in entries[:5]],
            }
        )
        print(f"{len(entries):4d} eligible / {pure} pure")

    ok = [r for r in rows if r["eligible"] >= 0]
    tot_el = sum(r["eligible"] for r in ok)
    tot_pure = sum(r["pure"] for r in ok)
    summary = {
        "repos": len(ok),
        "eligible": tot_el,
        "pure_total": tot_pure,
        "eligible_pct": round(100 * tot_el / max(tot_pure, 1), 2),
        "return_kinds": dict(ret_kinds),
        "exclusion_reasons": dict(reasons.most_common()),
    }
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    print("\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
