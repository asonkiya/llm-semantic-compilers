"""Corpus robustness + correctness harness — run `cgir scan` across a spread of
real public repositories (all five adapters) and check not just that it runs
but that it extracts what's actually there.

Scanning is fully static (tree-sitter; no repo dependencies to install), so we
can point it at anything. Each repo is pinned to a commit SHA so the
ground-truth denominator is reproducible (no HEAD drift → no flaky gate). For
each repo:

- scan under a timeout; record crash / timeout / success and wall time;
- **ground truth**: independently tree-sitter-parse the source and count the
  function-like definitions cgir *should* extract, then report
  extracted / present as an extraction ratio — the denominator that turns
  "1,513 components" into "1,513 of 1,580 (96%)" and would have flashed the
  #ifdef under-extraction (stb 16%) automatically;
- **determinism**: scan twice, require an identical component set;
- **downstream smoke**: run stats / search / impact on the index so the whole
  pipeline — not just ingest — is exercised on diverse real graphs.

Failures are captured and the sweep continues.

This is a regression *gate*, not just a report. Three modes:

    # report only (writes JSON, always exit 0)
    python benchmarks/corpus_scan.py --out corpus-report.json [--only flask,stb]

    # regenerate the committed baseline (run this when a ratio change is
    # intentional — e.g. an adapter improvement — and review the diff)
    python benchmarks/corpus_scan.py --update-baseline

    # CI gate: run and FAIL (exit 1) on any crash / timeout / non-determinism /
    # downstream failure, or an extraction ratio that dropped more than
    # `tolerance` below its baseline. This is what the nightly workflow runs.
    python benchmarks/corpus_scan.py --check
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from tree_sitter import Language, Parser


def _cgir_bin() -> str:
    """Prefer an explicit override, then the repo venv, then PATH (CI installs
    cgir system-wide, so `.venv/bin/cgir` won't exist there)."""
    import os

    if env := os.environ.get("CGIR_BIN"):
        return env
    venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "cgir"
    if venv.exists():
        return str(venv)
    return shutil.which("cgir") or str(venv)


CGIR = _cgir_bin()
BASELINE_PATH = Path(__file__).resolve().parent / "corpus-baseline.json"
# An extraction ratio may drop this far below baseline before it's a regression
# (absorbs tree-sitter grammar-wheel jitter; a real adapter break drops far more).
DEFAULT_TOLERANCE = 0.03

# name, git url, language, optional subdir (keeps giant repos tractable), pin SHA.
# Pins make the ground-truth denominator reproducible; refresh them alongside a
# deliberate `--update-baseline`.
CORPUS: list[tuple[str, str, str, str | None, str]] = [
    # Python — incl. a metaprogramming-heavy ORM and a numeric lib
    (
        "flask",
        "https://github.com/pallets/flask",
        "python",
        "src",
        "36e4a824f340fdee7ed50937ba8e7f6bc7d17f81",
    ),
    (
        "requests",
        "https://github.com/psf/requests",
        "python",
        "src",
        "69f84847045bef7a849cc994a26fe7ba8a169e95",
    ),
    (
        "click",
        "https://github.com/pallets/click",
        "python",
        "src",
        "cfa01eeb7894a408af70b29d28c0b24f8680f9fb",
    ),
    (
        "httpx",
        "https://github.com/encode/httpx",
        "python",
        "httpx",
        "b5addb64f0161ff6bfe94c124ef76f6a1fba5254",
    ),
    (
        "rich",
        "https://github.com/Textualize/rich",
        "python",
        "rich",
        "9d8f9a372cc5916fd4781fec207ced7ddac2f08f",
    ),
    (
        "sqlalchemy",
        "https://github.com/sqlalchemy/sqlalchemy",
        "python",
        "lib",
        "10cdc38ccf037617ef9baa4e816b5ff377f58a38",
    ),
    (
        "pydantic",
        "https://github.com/pydantic/pydantic",
        "python",
        "pydantic",
        "2294b52862478f3ef0fa0afd3cfdc9acba3881b0",
    ),
    (
        "django",
        "https://github.com/django/django",
        "python",
        "django",
        "76e1bca1311ae7073a1fa4add6f9d19d709f0f09",
    ),
    # TypeScript / JavaScript
    (
        "zod",
        "https://github.com/colinhacks/zod",
        "typescript",
        "src",
        "912f0f51b0ced654d0069741e7160834dca742ee",
    ),
    (
        "ky",
        "https://github.com/sindresorhus/ky",
        "typescript",
        "source",
        "3419113b48e034fdcf8fa6bd3be3da7b3d0d758f",
    ),
    (
        "axios",
        "https://github.com/axios/axios",
        "typescript",
        "lib",
        "c44f8d0a910df99486da9175584b99f56a94a73b",
    ),
    # Go — incl. a big framework
    (
        "cobra",
        "https://github.com/spf13/cobra",
        "go",
        None,
        "adbc8813901bba65827259daa8e22ff94ec1f30e",
    ),
    (
        "gin",
        "https://github.com/gin-gonic/gin",
        "go",
        None,
        "34dac209ffb6ef85cc78c5d217bbb7ad001d68fd",
    ),
    (
        "hugo",
        "https://github.com/gohugoio/hugo",
        "go",
        "hugolib",
        "89b8c322008f5285b81d4b357887292e3b61f708",
    ),
    # Rust — incl. async + macro-heavy
    (
        "ripgrep",
        "https://github.com/BurntSushi/ripgrep",
        "rust",
        "crates",
        "59e318f5ace48db54f37bb67c152535bc17fa153",
    ),
    (
        "clap",
        "https://github.com/clap-rs/clap",
        "rust",
        "clap_builder",
        "12e50b3bc855d506cdc07a3bdaece5416e6fc7ba",
    ),
    (
        "tokio",
        "https://github.com/tokio-rs/tokio",
        "rust",
        "tokio/src",
        "ac6869a431d9d7e2a81ce5309f00730741d3462a",
    ),
    # C — single-header, feature-flagged, and macro-dense
    (
        "tiny-AES-c",
        "https://github.com/kokke/tiny-AES-c",
        "c",
        None,
        "23856752fbd139da0b8ca6e471a13d5bcc99a08d",
    ),
    (
        "kilo",
        "https://github.com/antirez/kilo",
        "c",
        None,
        "323d93b29bd89a2cb446de90c4ed4fea1764176e",
    ),
    (
        "stb",
        "https://github.com/nothings/stb",
        "c",
        None,
        "31c1ad37456438565541f4919958214b6e762fb4",
    ),
    (
        "curl",
        "https://github.com/curl/curl",
        "c",
        "lib",
        "c5fd5eb55a58f0f370c69d56fd0dabfe76035474",
    ),
    (
        "redis",
        "https://github.com/redis/redis",
        "c",
        "src",
        "e1cc3dc268a5ff20eecbdb530483fd692c3dbc6e",
    ),
    ("jq", "https://github.com/jqlang/jq", "c", "src", "2d410d6d86be7f685ad28e5cffac0248aa47664c"),
]

_EXTS = {
    "python": {".py"},
    "typescript": {".ts", ".tsx", ".js"},
    "go": {".go"},
    "rust": {".rs"},
    "c": {".c", ".h"},
}

# tree-sitter node types that represent a function-like definition cgir should
# extract as a component. (TS arrow functions assigned to variables are a known
# adapter gap, counted separately and excluded from the strict ratio.)
_DEF_NODES = {
    "python": {"function_definition"},
    "typescript": {"function_declaration", "method_definition", "function_signature"},
    "go": {"function_declaration", "method_declaration"},
    "rust": {"function_item"},
    "c": {"function_definition"},
}
_TS_LANGS: dict[str, Language] = {}


def _language(lang: str) -> Language:
    if lang not in _TS_LANGS:
        if lang == "python":
            import tree_sitter_python as m

            _TS_LANGS[lang] = Language(m.language())
        elif lang == "typescript":
            import tree_sitter_typescript as m

            _TS_LANGS[lang] = Language(m.language_typescript())
        elif lang == "go":
            import tree_sitter_go as m

            _TS_LANGS[lang] = Language(m.language())
        elif lang == "rust":
            import tree_sitter_rust as m

            _TS_LANGS[lang] = Language(m.language())
        elif lang == "c":
            import tree_sitter_c as m

            _TS_LANGS[lang] = Language(m.language())
    return _TS_LANGS[lang]


def ground_truth_defs(scan_dir: Path, lang: str) -> tuple[int, int]:
    """(function-like definitions, LOC) counted independently via tree-sitter —
    the denominator for the extraction ratio."""
    parser = Parser(_language(lang))
    want = _DEF_NODES[lang]
    exts = _EXTS[lang]
    defs = loc = 0
    for f in scan_dir.rglob("*"):
        if f.suffix not in exts or not f.is_file():
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        loc += data.count(b"\n") + 1
        stack = [parser.parse(data).root_node]
        while stack:
            n = stack.pop()
            if n.type in want:
                defs += 1
            stack.extend(n.children)
    return defs, loc


def clone(name: str, url: str, cache: Path, pin: str | None) -> tuple[Path | None, str]:
    """Clone ``url`` into the cache, pinned to ``pin`` when given (so the
    ground-truth denominator is reproducible). GitHub serves arbitrary commit
    SHAs to `fetch`, so we init + fetch the one commit rather than clone HEAD."""
    dest = cache / name
    if dest.exists():
        # Trust the cache only if it's a *complete* checkout at the right
        # commit; a failed earlier fetch can leave an empty .git behind, which
        # would silently scan as 0 LOC (and, unpinned, a stale HEAD would
        # drift the denominator).
        head = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        ok = head.returncode == 0 and (pin is None or head.stdout.strip() == pin)
        if ok:
            return dest, ""
        shutil.rmtree(dest, ignore_errors=True)
    if pin:
        dest.mkdir(parents=True)
        steps = [
            ["git", "-C", str(dest), "init", "-q"],
            ["git", "-C", str(dest), "remote", "add", "origin", url],
            ["git", "-C", str(dest), "fetch", "--depth", "1", "-q", "origin", pin],
            ["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"],
        ]
        for step in steps:
            proc = subprocess.run(step, capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                return None, proc.stderr[-500:]
        return dest, ""
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return (dest, "") if proc.returncode == 0 else (None, proc.stderr[-500:])


def _component_ids(index: Path) -> set[str]:
    return {json.loads(p.read_text())["id"] for p in (index / "components").glob("*.json")}


def _run(argv: list[str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stderr or p.stdout)[-800:]
    except subprocess.TimeoutExpired:
        return -9, "timeout"


def scan_and_check(scan_dir: Path, lang: str, out: Path, timeout: int) -> dict:
    result: dict = {"scan_ok": False}
    t0 = time.monotonic()
    rc, err = _run([CGIR, "scan", str(scan_dir), "--out", str(out)], timeout)
    result["seconds"] = round(time.monotonic() - t0, 1)
    if rc == -9:
        result["scan"] = "timeout"
        return result
    if rc != 0:
        result["scan"] = "crash"
        result["stderr"] = err[-1500:]
        return result
    result["scan_ok"] = True

    comps = list((out / "components").glob("*.json"))
    kinds: dict[str, int] = {}
    langs: dict[str, int] = {}
    for p in comps:
        s = json.loads(p.read_text())
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        langs[s.get("language", "?")] = langs.get(s.get("language", "?"), 0) + 1
    graph = json.loads((out / "repo_graph.json").read_text())

    defs, loc = ground_truth_defs(scan_dir, lang)
    result["components"] = len(comps)
    result["ground_truth_defs"] = defs
    result["extraction_ratio"] = round(len(comps) / defs, 3) if defs else None
    result["loc"] = loc
    result["kinds"] = kinds
    result["languages"] = langs
    result["resolved_calls"] = sum(1 for e in graph.get("edges", []) if e.get("kind") == "CALLS")

    # determinism: a second scan must produce the identical component set
    out2 = out.parent / (out.name + "_2")
    shutil.rmtree(out2, ignore_errors=True)
    rc2, _ = _run([CGIR, "scan", str(scan_dir), "--out", str(out2)], timeout)
    result["deterministic"] = rc2 == 0 and _component_ids(out) == _component_ids(out2)
    shutil.rmtree(out2, ignore_errors=True)

    # downstream smoke: exercise the whole pipeline, not just ingest, on this
    # real graph — stats, structured search, and impact (blast radius) on a
    # sampled component.
    downstream = {
        "stats": _run([CGIR, "stats", "--index", str(out), "--json"], 120)[0] == 0,
        "search": _run([CGIR, "search", "effects:io", "--index", str(out)], 120)[0] == 0,
    }
    if comps:
        sample = json.loads(comps[0].read_text())["id"]
        downstream["impact"] = _run([CGIR, "impact", sample, "--index", str(out)], 120)[0] == 0
    result["downstream_ok"] = all(downstream.values())
    result["downstream"] = downstream
    return result


def run_corpus(picks: set[str] | None, cache: Path, timeout: int) -> list[dict]:
    cache.mkdir(parents=True, exist_ok=True)
    work = cache / "_indexes"
    work.mkdir(exist_ok=True)
    rows: list[dict] = []
    for name, url, lang, subdir, pin in CORPUS:
        if picks and name not in picks:
            continue
        row: dict = {"name": name, "lang": lang, "url": url, "pin": pin}
        print(f"[{lang:10s}] {name:14s}", flush=True, end=" ")
        try:
            repo, err = clone(name, url, cache, pin)
            if repo is None:
                row["clone"] = "fail"
                row["error"] = err
                print("CLONE FAIL")
                rows.append(row)
                continue
            scan_dir = repo / subdir if subdir and (repo / subdir).exists() else repo
            idx = work / name
            shutil.rmtree(idx, ignore_errors=True)
            row.update(scan_and_check(scan_dir, lang, idx, timeout))
            if row.get("scan_ok"):
                print(
                    f"{row['loc'] // 1000}k LOC -> OK {row['seconds']}s | "
                    f"{row['components']}/{row['ground_truth_defs']} defs "
                    f"({row['extraction_ratio']}) | det={row['deterministic']} "
                    f"down={row['downstream_ok']}",
                    flush=True,
                )
            else:
                print(row.get("scan", "?").upper(), flush=True)
        except Exception:
            row["scan"] = "harness-error"
            row["error"] = traceback.format_exc()[-1500:]
            print("HARNESS ERROR")
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("scan_ok")]
    low = [
        f"{r['name']} ({r['extraction_ratio']})"
        for r in ok
        if r.get("extraction_ratio") is not None and r["extraction_ratio"] < 0.85
    ]
    return {
        "repos": len(rows),
        "scanned_ok": len(ok),
        "crashed": [r["name"] for r in rows if r.get("scan") == "crash"],
        "timed_out": [r["name"] for r in rows if r.get("scan") == "timeout"],
        "clone_failed": [r["name"] for r in rows if r.get("clone") == "fail"],
        "non_deterministic": [r["name"] for r in ok if not r.get("deterministic")],
        "downstream_failures": [r["name"] for r in ok if not r.get("downstream_ok")],
        "low_extraction": low,
        "total_loc": sum(r.get("loc", 0) for r in ok),
        "total_components": sum(r.get("components", 0) for r in ok),
        "total_defs": sum(r.get("ground_truth_defs", 0) for r in ok),
    }


def write_baseline(rows: list[dict], tolerance: float) -> None:
    """Freeze the current per-repo ratios as the regression baseline."""
    repos = {
        r["name"]: {
            "lang": r["lang"],
            "pin": r.get("pin"),
            "extraction_ratio": r["extraction_ratio"],
            "components": r["components"],
            "ground_truth_defs": r["ground_truth_defs"],
        }
        for r in rows
        if r.get("scan_ok") and r.get("extraction_ratio") is not None
    }
    BASELINE_PATH.write_text(json.dumps({"tolerance": tolerance, "repos": repos}, indent=2) + "\n")
    print(f"\nwrote baseline: {len(repos)} repos -> {BASELINE_PATH}")


def check_against_baseline(rows: list[dict], baseline: dict) -> list[str]:
    """Return a list of human-readable regression failures (empty = pass).

    ``baseline`` is the loaded ``corpus-baseline.json`` dict (``{tolerance,
    repos}``); pass it in so the gate logic is testable without the filesystem."""
    tol = baseline.get("tolerance", DEFAULT_TOLERANCE)
    base_repos = baseline["repos"]
    failures: list[str] = []
    for r in rows:
        name = r["name"]
        # A clone failure is infrastructure/network noise, not an adapter
        # regression — report it but don't fail the gate on it (keeps the
        # nightly run from flaking on a transient fetch).
        if r.get("clone") == "fail":
            print(f"  note: {name} clone failed (infra, not gated)", flush=True)
            continue
        # Everything below is a real adapter/pipeline signal.
        if not r.get("scan_ok"):
            failures.append(f"{name}: scan {r.get('scan', '?')}")
            continue
        if not r.get("deterministic"):
            failures.append(f"{name}: non-deterministic (two scans differ)")
        if not r.get("downstream_ok"):
            failures.append(f"{name}: downstream failed {r.get('downstream')}")
        # Ratio regression vs the frozen baseline.
        base = base_repos.get(name)
        if base is None:
            print(f"  note: {name} not in baseline (skipping ratio check)", flush=True)
            continue
        got, want = r.get("extraction_ratio"), base["extraction_ratio"]
        if got is not None and want is not None and got < want - tol:
            failures.append(
                f"{name}: extraction ratio regressed {want} -> {got} "
                f"(floor {round(want - tol, 3)}); components {base['ground_truth_defs']}"
                f"-denom now {r['components']}/{r['ground_truth_defs']}"
            )
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("benchmarks/corpus-report.json"))
    ap.add_argument("--cache", type=Path, default=Path("/tmp/cgir-corpus"))
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", default=None)
    ap.add_argument(
        "--check", action="store_true", help="Gate: exit 1 on regression vs the baseline."
    )
    ap.add_argument(
        "--update-baseline", action="store_true", help="Freeze current ratios as the baseline."
    )
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    args = ap.parse_args()

    picks = set(args.only.split(",")) if args.only else None
    rows = run_corpus(picks, args.cache, args.timeout)
    summary = summarize(rows)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    print("\n" + json.dumps(summary, indent=2))

    if args.update_baseline:
        write_baseline(rows, args.tolerance)
        return
    if args.check:
        if not BASELINE_PATH.exists():
            print(f"\nno baseline at {BASELINE_PATH} (run --update-baseline first)")
            sys.exit(1)
        failures = check_against_baseline(rows, json.loads(BASELINE_PATH.read_text()))
        if failures:
            print("\nCORPUS GATE FAILED:")
            for f in failures:
                print(f"  ✗ {f}")
            sys.exit(1)
        print("\nCORPUS GATE PASSED — no regressions vs baseline.")


if __name__ == "__main__":
    main()
