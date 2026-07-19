"""Corpus robustness harness — run `cgir scan` across a spread of real public
repositories (all five adapters) and report how it holds up.

Scanning is fully static (tree-sitter; no repo dependencies to install), so
we can point it at anything. For each repo: shallow-clone, scan under a
timeout, and record — did it finish, crash, or time out; wall time; component
counts and kind/purity/effect distribution; and resolved-call ratio (a proxy
for symbol-resolution quality). Failures are captured and the sweep
continues, so one bad repo can't sink the run.

    python benchmarks/corpus_scan.py --out corpus-report.json [--only flask,zod]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import traceback
from pathlib import Path

CGIR = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "cgir")

# name, git url, language, optional subdir to scan (keeps giant repos tractable)
CORPUS = [
    # Python
    ("flask", "https://github.com/pallets/flask", "python", "src"),
    ("requests", "https://github.com/psf/requests", "python", "src"),
    ("click", "https://github.com/pallets/click", "python", "src"),
    ("httpx", "https://github.com/encode/httpx", "python", "httpx"),
    ("rich", "https://github.com/Textualize/rich", "python", "rich"),
    # TypeScript / JavaScript
    ("zod", "https://github.com/colinhacks/zod", "typescript", "src"),
    ("ky", "https://github.com/sindresorhus/ky", "typescript", "source"),
    # Go
    ("cobra", "https://github.com/spf13/cobra", "go", None),
    ("gin", "https://github.com/gin-gonic/gin", "go", None),
    # Rust
    ("ripgrep", "https://github.com/BurntSushi/ripgrep", "rust", "crates"),
    ("clap", "https://github.com/clap-rs/clap", "rust", "clap_builder"),
    # C
    ("tiny-AES-c", "https://github.com/kokke/tiny-AES-c", "c", None),
    ("kilo", "https://github.com/antirez/kilo", "c", None),
    ("stb", "https://github.com/nothings/stb", "c", None),
    ("curl", "https://github.com/curl/curl", "c", "lib"),
]


def clone(name: str, url: str, cache: Path) -> tuple[Path | None, str]:
    dest = cache / name
    if dest.exists():
        return dest, ""
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        return None, proc.stderr[-500:]
    return dest, ""


def _count_source(path: Path, exts: set[str]) -> tuple[int, int]:
    import contextlib

    files = [p for p in path.rglob("*") if p.suffix in exts and p.is_file()]
    loc = 0
    for f in files:
        with contextlib.suppress(OSError):
            loc += sum(1 for _ in f.open("rb"))
    return len(files), loc


_EXTS = {
    "python": {".py"},
    "typescript": {".ts", ".tsx", ".js"},
    "go": {".go"},
    "rust": {".rs"},
    "c": {".c", ".h"},
}


def scan_and_analyze(repo: Path, scan_dir: Path, out: Path, timeout: int) -> dict:
    result: dict = {"scan_ok": False}
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [CGIR, "scan", str(scan_dir), "--out", str(out)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["scan"] = "timeout"
        result["seconds"] = timeout
        return result
    result["seconds"] = round(time.monotonic() - t0, 1)
    if proc.returncode != 0:
        result["scan"] = "crash"
        result["stderr"] = proc.stderr[-1500:]
        return result
    result["scan_ok"] = True
    # aggregate from the index
    comps = list((out / "components").glob("*.json"))
    kinds: dict[str, int] = {}
    langs: dict[str, int] = {}
    pure = 0
    for p in comps:
        s = json.loads(p.read_text())
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        langs[s.get("language", "?")] = langs.get(s.get("language", "?"), 0) + 1
        if s["kind"] == "pure_function":
            pure += 1
    graph = json.loads((out / "repo_graph.json").read_text())
    call_edges = sum(1 for e in graph.get("edges", []) if e.get("kind") == "CALLS")
    result["components"] = len(comps)
    result["kinds"] = kinds
    result["languages"] = langs
    result["pure_pct"] = round(100 * pure / len(comps), 1) if comps else 0
    result["resolved_calls"] = call_edges
    result["nodes"] = len(graph.get("nodes", []))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=Path("/tmp/cgir-corpus"))
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--only", default=None, help="comma-separated repo names")
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
        print(f"[{lang:10s}] {name:14s} cloning...", flush=True, end=" ")
        try:
            repo, err = clone(name, url, args.cache)
            if repo is None:
                row["clone"] = "fail"
                row["error"] = err
                print("CLONE FAIL")
                rows.append(row)
                continue
            scan_dir = repo / subdir if subdir else repo
            if not scan_dir.exists():
                scan_dir = repo
            files, loc = _count_source(scan_dir, _EXTS.get(lang, set()))
            row["files"] = files
            row["loc"] = loc
            idx = work / name
            shutil.rmtree(idx, ignore_errors=True)
            res = scan_and_analyze(repo, scan_dir, idx, args.timeout)
            row.update(res)
            status = "OK" if res.get("scan_ok") else res.get("scan", "?").upper()
            print(
                f"{files} files/{loc // 1000}k LOC -> {status} "
                f"{res.get('seconds', '?')}s, {res.get('components', 0)} comps",
                flush=True,
            )
        except Exception:
            row["scan"] = "harness-error"
            row["error"] = traceback.format_exc()[-1500:]
            print("HARNESS ERROR")
        rows.append(row)

    ok = [r for r in rows if r.get("scan_ok")]
    summary = {
        "repos": len(rows),
        "scanned_ok": len(ok),
        "crashed": [r["name"] for r in rows if r.get("scan") == "crash"],
        "timed_out": [r["name"] for r in rows if r.get("scan") == "timeout"],
        "clone_failed": [r["name"] for r in rows if r.get("clone") == "fail"],
        "total_loc": sum(r.get("loc", 0) for r in ok),
        "total_components": sum(r.get("components", 0) for r in ok),
    }
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    print("\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
