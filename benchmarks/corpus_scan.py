"""Corpus robustness + correctness harness — run `cgir scan` across a spread of
real public repositories (all five adapters) and check not just that it runs
but that it extracts what's actually there.

Scanning is fully static (tree-sitter; no repo dependencies to install), so we
can point it at anything. For each repo:

- scan under a timeout; record crash / timeout / success and wall time;
- **ground truth**: independently tree-sitter-parse the source and count the
  function-like definitions cgir *should* extract, then report
  extracted / present as an extraction ratio — the denominator that turns
  "1,513 components" into "1,513 of 1,580 (96%)" and would have flashed the
  #ifdef under-extraction (stb 16%) automatically;
- **determinism**: scan twice, require an identical component set;
- **downstream smoke**: run stats / search / decompose on the index so the
  whole pipeline — not just ingest — is exercised on diverse real graphs.

Failures are captured and the sweep continues.

    python benchmarks/corpus_scan.py --out corpus-report.json [--only flask,stb]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from tree_sitter import Language, Parser

CGIR = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "cgir")

# name, git url, language, optional subdir (keeps giant repos tractable)
CORPUS = [
    # Python — incl. a metaprogramming-heavy ORM and a numeric lib
    ("flask", "https://github.com/pallets/flask", "python", "src"),
    ("requests", "https://github.com/psf/requests", "python", "src"),
    ("click", "https://github.com/pallets/click", "python", "src"),
    ("httpx", "https://github.com/encode/httpx", "python", "httpx"),
    ("rich", "https://github.com/Textualize/rich", "python", "rich"),
    ("sqlalchemy", "https://github.com/sqlalchemy/sqlalchemy", "python", "lib"),
    ("pydantic", "https://github.com/pydantic/pydantic", "python", "pydantic"),
    ("django", "https://github.com/django/django", "python", "django"),
    # TypeScript / JavaScript
    ("zod", "https://github.com/colinhacks/zod", "typescript", "src"),
    ("ky", "https://github.com/sindresorhus/ky", "typescript", "source"),
    ("axios", "https://github.com/axios/axios", "typescript", "lib"),
    # Go — incl. a big framework
    ("cobra", "https://github.com/spf13/cobra", "go", None),
    ("gin", "https://github.com/gin-gonic/gin", "go", None),
    ("hugo", "https://github.com/gohugoio/hugo", "go", "hugolib"),
    # Rust — incl. async + macro-heavy
    ("ripgrep", "https://github.com/BurntSushi/ripgrep", "rust", "crates"),
    ("clap", "https://github.com/clap-rs/clap", "rust", "clap_builder"),
    ("tokio", "https://github.com/tokio-rs/tokio", "rust", "tokio/src"),
    # C — single-header, feature-flagged, and macro-dense
    ("tiny-AES-c", "https://github.com/kokke/tiny-AES-c", "c", None),
    ("kilo", "https://github.com/antirez/kilo", "c", None),
    ("stb", "https://github.com/nothings/stb", "c", None),
    ("curl", "https://github.com/curl/curl", "c", "lib"),
    ("redis", "https://github.com/redis/redis", "c", "src"),
    ("jq", "https://github.com/jqlang/jq", "c", "src"),
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


def clone(name: str, url: str, cache: Path) -> tuple[Path | None, str]:
    dest = cache / name
    if dest.exists():
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=Path("/tmp/cgir-corpus"))
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    work = args.cache / "_indexes"
    work.mkdir(exist_ok=True)
    picks = set(args.only.split(",")) if args.only else None

    rows = []
    for name, url, lang, subdir in CORPUS:
        if picks and name not in picks:
            continue
        row: dict = {"name": name, "lang": lang, "url": url}
        print(f"[{lang:10s}] {name:14s}", flush=True, end=" ")
        try:
            repo, err = clone(name, url, args.cache)
            if repo is None:
                row["clone"] = "fail"
                row["error"] = err
                print("CLONE FAIL")
                rows.append(row)
                continue
            scan_dir = repo / subdir if subdir and (repo / subdir).exists() else repo
            idx = work / name
            shutil.rmtree(idx, ignore_errors=True)
            row.update(scan_and_check(scan_dir, lang, idx, args.timeout))
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

    ok = [r for r in rows if r.get("scan_ok")]
    low = [
        f"{r['name']} ({r['extraction_ratio']})"
        for r in ok
        if r.get("extraction_ratio") is not None and r["extraction_ratio"] < 0.85
    ]
    summary = {
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
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    print("\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
