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
    build_def_index,
    build_multi_index,
    extract_definition,
    lift_symbol,
    lift_symbols,
    resolve_includes,
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


def sweep_file(
    source: Path,
    index: Path,
    shim: str,
    workdir: Path,
    pointers: bool,
    non_leaf: bool,
    structs: bool,
    tu_dir: Path | None,
    include_dirs: list[Path] | None = None,
    hdr_cache: dict | None = None,
) -> dict:
    """Sweep one .c file: worklist -> per-function baseline vs lifted compile.

    With ``structs``, single-level struct-pointer functions are included and the
    worklist's extracted struct layouts (``entry.struct_defs`` — what the real
    pipeline prepends) are fed into the compile as extra shim, so a function is
    counted compilable iff its struct context is in-file-resolvable.

    With ``include_dirs``, the LIFTED side resolves definitions across the
    file's #include closure (struct layouts / static-inline helpers / macros in
    headers) and gets NO struct_defs handout — header-aware lifting must pull
    the struct context itself, so the delta measures exactly that. Baseline is
    unchanged (function + shim + handed struct layouts)."""
    src_text = source.read_text(errors="replace")
    entries, excluded = c_rust_worklist(
        index, source, pointers=pointers, include_nonleaf=non_leaf, structs=structs
    )
    masked = _mask_comments(src_text)
    # One pass builds the whole name->definition index; every per-function
    # extract/lift below is then an O(1) lookup instead of a fresh 9.5 MB scan
    # (without this the struct sweep's ~600 functions rebuild it ~600x).
    index = build_def_index(src_text, masked)
    multi_index = ranks = None
    if include_dirs:
        files = resolve_includes(source, include_dirs)
        multi_index, ranks = build_multi_index(files, cache=hdr_cache)
    rows = []
    for e in entries:
        # struct-pointer functions need their layout; hand the worklist's
        # extracted defs to both baseline and lifted so the delta isolates
        # lifting's table/helper contribution, not the struct context.
        struct_ctx = "\n\n".join(e.struct_defs[k] for k in sorted(e.struct_defs))
        e_shim = shim + ("\n\n" + struct_ctx if struct_ctx else "")
        d = extract_definition(e.name, src_text, masked, index=index)
        base_ok, _ = (
            _compiles(e_shim + "\n" + d + "\n", workdir, f"base_{source.stem}_{e.name}")
            if d
            else (False, "")
        )
        if multi_index is not None:
            tu, unresolved, _missing = lift_symbols(
                src_text, [e.name], shim=shim, index=multi_index, ranks=ranks
            )
        else:
            tu, unresolved = lift_symbol(src_text, e.name, shim=e_shim, masked=masked, index=index)
        lift_ok, lift_err = (
            _compiles(tu, workdir, f"lift_{source.stem}_{e.name}")
            if tu
            else (False, "lift returned None")
        )
        if lift_ok and tu_dir and tu:
            (tu_dir / f"{source.stem}__{e.name}.c").write_text(tu)
        rows.append(
            {
                "file": source.name,
                "name": e.name,
                "ret": e.ret,
                "params": e.params,
                "is_struct": bool(e.struct_defs),
                "baseline_ok": base_ok,
                "lifted_ok": lift_ok,
                "tu_lines": len(tu.splitlines()) if tu else 0,
                "unresolved": unresolved[:12],
                "error": lift_err,
            }
        )
    return {"rows": rows, "excluded": excluded}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, help="a single .c file to sweep")
    ap.add_argument(
        "--source-dir",
        type=Path,
        help="sweep every .c under this dir (aggregate across the tree, e.g. the kernel's crypto/)",
    )
    ap.add_argument("--index", type=Path, help="existing cgir index (else scans the source tree)")
    ap.add_argument("--shim", type=Path, help="shim header text file (default: built-in)")
    ap.add_argument("--out", type=Path, help="write the full JSON report here")
    ap.add_argument("--tu-dir", type=Path, help="write compilable lifted TUs here")
    ap.add_argument("--non-leaf", action="store_true", help="include non-leaf functions")
    ap.add_argument(
        "--pointers", action="store_true", help="include char*/byte-buffer pointer ABIs"
    )
    ap.add_argument(
        "--structs",
        action="store_true",
        help="include single-level struct-pointer ABIs (the worklist's struct layouts "
        "are supplied to the compile; measures how much of the struct wall is "
        "in-file-resolvable)",
    )
    ap.add_argument(
        "--include",
        type=Path,
        action="append",
        default=None,
        help="header-aware lifting: resolve definitions across each file's #include "
        "closure rooted here (repeatable, e.g. linux/include + arch includes)",
    )
    args = ap.parse_args()
    if not (args.source or args.source_dir):
        ap.error("one of --source or --source-dir is required")

    shim = args.shim.read_text() if args.shim else DEFAULT_SHIM
    scan_root = args.source_dir or args.source.parent

    index = args.index
    if index is None:
        index = Path(tempfile.mkdtemp(prefix="c-lift-sweep-idx-"))
        print(f"scanning {scan_root} ...", flush=True)
        scan_repo(scan_root, out=index)

    files = sorted(args.source_dir.rglob("*.c")) if args.source_dir else [args.source]
    print(f"sweeping {len(files)} file(s) under {scan_root}", flush=True)

    workdir = Path(tempfile.mkdtemp(prefix="c-lift-sweep-cc-"))
    if args.tu_dir:
        args.tu_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    excluded: list = []
    hdr_cache: dict = {}  # shared across files: each header indexed once
    t0 = time.time()
    for i, f in enumerate(files):
        try:
            r = sweep_file(
                f,
                index,
                shim,
                workdir,
                args.pointers,
                args.non_leaf,
                args.structs,
                args.tu_dir,
                include_dirs=args.include,
                hdr_cache=hdr_cache,
            )
        except Exception as exc:  # a single unparseable file must not sink the sweep
            print(f"  ! {f.name}: {type(exc).__name__}: {exc}", flush=True)
            continue
        rows.extend(r["rows"])
        excluded.extend(r["excluded"])
        if len(files) > 1 and (i + 1) % 20 == 0:
            bl = sum(x["baseline_ok"] for x in rows)
            lf = sum(x["lifted_ok"] for x in rows)
            print(
                f"  {i + 1}/{len(files)} files  {len(rows)} fns  baseline {bl}  lifted {lf}"
                f"  ({time.time() - t0:.0f}s)",
                flush=True,
            )

    baseline_ok = sum(x["baseline_ok"] for x in rows)
    lifted_ok = sum(x["lifted_ok"] for x in rows)
    newly = [f"{r['file']}:{r['name']}" for r in rows if r["lifted_ok"] and not r["baseline_ok"]]
    regressed = [
        f"{r['file']}:{r['name']}" for r in rows if r["baseline_ok"] and not r["lifted_ok"]
    ]
    report = {
        "root": str(scan_root),
        "files": len(files),
        "worklist": len(rows),
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

    label = str(args.source_dir) if args.source_dir else args.source.name
    abi = "scalar" + ("/pointer" if args.pointers else "") + ("/struct" if args.structs else "")
    n_struct = sum(x.get("is_struct", False) for x in rows)
    n_struct_lift = sum(x.get("is_struct", False) and x["lifted_ok"] for x in rows)
    print(f"\n=== sweep: {label} ({len(files)} file(s)) ===")
    print(f"{abi}-ABI pure functions : {len(rows)}")
    print(f"baseline compilable       : {baseline_ok}")
    print(f"lifted compilable         : {lifted_ok}")
    print(f"newly unlocked by lifting : {len(newly)}")
    print(f"regressed by lifting      : {len(regressed)}  {regressed[:8]}")
    if args.structs:
        print(f"struct-ptr functions      : {n_struct}  (lift-compilable: {n_struct_lift})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
