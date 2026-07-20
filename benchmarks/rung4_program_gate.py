"""Whole-program gate for the C->Rust sweep — capture/replay applied to
context-dependent (memory-path) functions.

The per-function differential feeds the C original and the Rust candidate
*identical random buffers*, so a function whose contract depends on hidden
state (e.g. ``sqlite3MemSize`` reading the allocation-size word *before* its
pointer) passes: both read the same garbage and agree. It then corrupts a
live engine.

You cannot replay a recorded *pointer* (its address and heap are gone). So
replay the real *workload* instead: link the candidate into a real SQLite,
one function at a time, and run the SQL battery. A function that survives —
byte-identical to stock, no crash — is whole-program-safe; one that crashes
or diverges is rejected, no name heuristic required. This is the actual gate;
the isolated differential is a cheap pre-filter.

    python benchmarks/rung4_program_gate.py \
        --results benchmarks/rung4-nonleaf-sqlite.json \
        --src <sqlite-src> --index <sqlite-idx> --out gate-report.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from cgir.rewrite_c_rust import (
    _build_rust_staticlib,
    _patch_source,
    c_rust_worklist,
    extern_block,
    link_back,
    suspect_global_reads,
)

sys.path.insert(0, str(Path(__file__).parent))
from rung4_nonleaf_battery import CFLAGS, SQL_BATTERY


def _build_shell(src: Path, shell_c: Path, amalg: Path, out: Path, staticlib: Path | None) -> bool:
    cmd = ["cc", *CFLAGS, str(shell_c), str(amalg), "-o", str(out), "-lm", "-lpthread"]
    if staticlib is not None:
        cmd.append(f"-Wl,-force_load,{staticlib}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600).returncode == 0


def _run_battery(shell: Path, db: Path) -> tuple[int, str]:
    db.unlink(missing_ok=True)
    p = subprocess.run(
        [str(shell), str(db)], input=SQL_BATTERY, capture_output=True, text=True, timeout=120
    )
    return p.returncode, p.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    src = args.src / "sqlite3.c"
    shell_c = args.src / "shell.c"
    worklist = c_rust_worklist(args.index, src, pointers=True, include_nonleaf=True)[0]
    by_id = {e.component_id: e for e in worklist}
    by_name = {e.name: e for e in worklist}
    report = json.loads(args.results.read_text())
    cand: dict[str, str] = {}
    for o in report["outcomes"]:
        if o["status"] == "solved":
            e = by_id.get(o["component_id"])
            if e is not None:
                cand[e.name] = next(a["candidate"] for a in o["attempts"] if a["stage"] == "ok")

    wd = Path(tempfile.mkdtemp(prefix="cgir-gate-"))
    stock = wd / "stock"
    if not _build_shell(src, shell_c, src, stock, None):
        raise SystemExit("stock shell build failed")
    stock_rc, stock_out = _run_battery(stock, wd / "stock.db")
    assert stock_rc == 0, "stock battery should not crash"

    verified: list[str] = []
    rejected: dict[str, str] = {}
    skipped_state: list[str] = []
    for i, name in enumerate(sorted(cand)):
        e = by_name[name]
        if suspect_global_reads(e):
            skipped_state.append(name)
            continue
        d = wd / f"f{i}"
        d.mkdir()
        callees = [by_name[c] for c in e.callees if c in by_name]
        sl = _build_rust_staticlib({name: cand[name]}, d, extern_block(callees))
        patched = _patch_source(src, [name], d, also_export={c.name for c in callees})
        sh = d / "sh"
        if not _build_shell(src, shell_c, patched, sh, sl):
            rejected[name] = "build_fail"
        else:
            rc, out = _run_battery(sh, d / "r.db")
            if rc != 0:
                rejected[name] = f"crash(rc={rc})"
            elif out != stock_out:
                rejected[name] = "diverged"
            else:
                verified.append(name)
        verdict = "ok" if name in verified else rejected.get(name, "?")
        print(f"[{i + 1:3d}/{len(cand)}] {name:34s} {verdict}", flush=True)

    # link the full verified set and confirm the assembled battery passes
    winners = {n: cand[n] for n in verified}
    g = link_back(src, winners, args.out.parent / "verified-link", CFLAGS, entries=worklist)
    final = "n/a"
    if g["linked"]:
        vsh = args.out.parent / "verified-link" / "sh"
        if _build_shell(src, shell_c, Path(g["patched_source"]), vsh, Path(g["staticlib"])):
            rc, out = _run_battery(vsh, wd / "v.db")
            final = "IDENTICAL" if rc == 0 and out == stock_out else f"FAIL(rc={rc})"

    summary = {
        "candidates": len(cand),
        "skipped_state_readers": len(skipped_state),
        "gated": len(cand) - len(skipped_state),
        "verified": len(verified),
        "rejected": rejected,
        "assembled_verified_battery": final,
    }
    args.out.write_text(
        json.dumps({"summary": summary, "verified": sorted(verified)}, indent=2) + "\n"
    )
    print("\n" + json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
