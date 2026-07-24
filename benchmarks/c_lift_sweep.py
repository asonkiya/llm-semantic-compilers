"""Sweep a single-file C library / amalgamation with the lifter.

For every ABI-eligible pure function in the target file, try to produce a
standalone compilable TU two ways:

- **baseline**: the function's own definition + shim (the pre-lifter state);
- **lifted**: ``lift_symbol`` — the function plus every file-scope definition
  it transitively references (tables, ``#define``s, consts, helpers).

The delta between the two is the lifter's contribution; the lifted-ok set is
the file's one-command-rewritable surface (each TU is a valid ``--c-source``
for ``cgir rewrite --lang c-rust``). Writes lifted TUs for the compilable set
to ``--tu-dir`` so a live-rewrite sample can run straight off the sweep.

Usage:
    python benchmarks/c_lift_sweep.py --source path/to/sqlite3.c \
        --out sweep.json --tu-dir /tmp/tus [--shim path/to/shim.h]

The default shim covers the sqlite/kernel-style scalar typedefs and neuters
annotation macros; it deliberately does NOT define any function the C source
should provide — an unresolved symbol must fail the compile, not be papered
over.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cgir.ffi.sources.c import c_rust_worklist
from cgir.ffi.sources.c_lift import (
    DEFAULT_SHIM as _BASE_SHIM,
)
from cgir.ffi.sources.c_lift import (
    _mask_comments,
    extract_definition,
    lift_symbol,
)
from cgir.pipeline import scan_repo

# The module shim plus the sqlite-specific scalar spellings the ABI layer
# accepts (most also self-resolve via in-file typedef pulling; naming them here
# keeps the sweep's unresolved lists focused on real gaps).
DEFAULT_SHIM = (
    _BASE_SHIM
    + """\
typedef int64_t sqlite3_int64; typedef uint64_t sqlite3_uint64;
typedef int64_t sqlite_int64; typedef uint64_t sqlite_uint64;
typedef int16_t LogEst; typedef uint64_t tRowcnt; typedef int Bool;
"""
)


def _compiles(c_text: str, workdir: Path, name: str) -> tuple[bool, str]:
    cf = workdir / f"{name}.c"
    cf.write_text(c_text)
    r = subprocess.run(
        ["cc", "-c", "-w", str(cf), "-o", "/dev/null"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    err = (r.stderr.strip().splitlines() or [""])[-1][:100] if r.returncode else ""
    return r.returncode == 0, err


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, required=True, help="the .c file to sweep")
    ap.add_argument("--index", type=Path, help="existing cgir index (else scans --source's dir)")
    ap.add_argument("--shim", type=Path, help="shim header text file (default: built-in)")
    ap.add_argument("--out", type=Path, help="write the full JSON report here")
    ap.add_argument("--tu-dir", type=Path, help="write compilable lifted TUs here")
    ap.add_argument("--non-leaf", action="store_true", help="include non-leaf functions")
    args = ap.parse_args()

    src_text = args.source.read_text(errors="replace")
    shim = args.shim.read_text() if args.shim else DEFAULT_SHIM

    index = args.index
    if index is None:
        index = Path(tempfile.mkdtemp(prefix="c-lift-sweep-idx-"))
        print(f"scanning {args.source.parent} ...", flush=True)
        scan_repo(args.source.parent, out=index)

    entries, excluded = c_rust_worklist(
        index, args.source, pointers=False, include_nonleaf=args.non_leaf
    )
    print(f"worklist: {len(entries)} scalar-ABI pure functions, {len(excluded)} excluded")

    # shared across every lift of this source: one mask, one extraction cache
    t0 = time.time()
    masked = _mask_comments(src_text)
    print(f"masked {len(src_text) / 1e6:.1f} MB in {time.time() - t0:.1f}s", flush=True)
    cache: dict[str, str | None] = {}

    workdir = Path(tempfile.mkdtemp(prefix="c-lift-sweep-cc-"))
    if args.tu_dir:
        args.tu_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    baseline_ok = lifted_ok = 0
    t0 = time.time()
    for i, e in enumerate(entries):
        # baseline: the function definition alone (+shim)
        d = extract_definition(e.name, src_text, masked)
        base_ok, _ = (
            _compiles(shim + "\n" + d + "\n", workdir, f"base_{e.name}") if d else (False, "")
        )
        # lifted: the function + its transitive in-file deps
        tu, unresolved = lift_symbol(src_text, e.name, shim=shim, masked=masked, cache=cache)
        lift_ok, lift_err = (
            _compiles(tu, workdir, f"lift_{e.name}") if tu else (False, "lift returned None")
        )
        baseline_ok += base_ok
        lifted_ok += lift_ok
        if lift_ok and args.tu_dir and tu:
            (args.tu_dir / f"{e.name}.c").write_text(tu)
        rows.append(
            {
                "name": e.name,
                "ret": e.ret,
                "params": e.params,
                "baseline_ok": base_ok,
                "lifted_ok": lift_ok,
                "tu_lines": len(tu.splitlines()) if tu else 0,
                "unresolved": unresolved[:12],
                "error": lift_err,
            }
        )
        if (i + 1) % 25 == 0:
            print(
                f"  {i + 1}/{len(entries)}  baseline {baseline_ok}  lifted {lifted_ok}"
                f"  ({time.time() - t0:.0f}s)",
                flush=True,
            )

    newly = [r["name"] for r in rows if r["lifted_ok"] and not r["baseline_ok"]]
    regressed = [r["name"] for r in rows if r["baseline_ok"] and not r["lifted_ok"]]
    report = {
        "source": str(args.source),
        "source_lines": src_text.count("\n"),
        "worklist": len(entries),
        "excluded": len(excluded),
        "baseline_ok": baseline_ok,
        "lifted_ok": lifted_ok,
        "newly_unlocked": newly,
        "regressed": regressed,
        "rows": rows,
        "excluded_reasons": excluded,
    }
    if args.out:
        args.out.write_text(json.dumps(report, indent=1))

    print(f"\n=== sweep: {args.source.name} ({src_text.count(chr(10)):,} lines) ===")
    print(f"scalar-ABI pure functions : {len(entries)}")
    print(f"baseline compilable       : {baseline_ok}")
    print(f"lifted compilable         : {lifted_ok}")
    print(f"newly unlocked by lifting : {len(newly)}")
    print(f"regressed by lifting      : {len(regressed)}  {regressed[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
