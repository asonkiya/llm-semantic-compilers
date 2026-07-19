"""Link-back: SQLite rebuilt with the Rust rewrites *inside*.

The "plugged in seamlessly" step the vision demands. Takes a rung-4
results file, and:

1. Concatenates the winning Rust functions into one staticlib.
2. Patches the amalgamation: de-statics each replaced symbol and renames
   its C *definition* to ``<name>__cgir_replaced`` — call sites still
   reference ``<name>``, which now resolves at link time to the Rust
   implementation.
3. Builds two real sqlite3 shells with identical flags: stock, and
   C-with-Rust-inside.
4. Runs a SQL battery chosen to exercise the replaced functions
   (tokenizer identifier classing, hex literals, LIKE, FTS5 MATCH,
   int/float comparison edges, CAST double->int, varint-heavy storage,
   ORDER BY planning, integrity_check) and diffs the two shells'
   output byte-for-byte.

Exclusions: state-dependent winners (the rung-4 audit's vacuous class)
must NOT be linked — their Rust is only equivalent in the untouched
state. Currently: sqlite3HeapNearlyFull.

    .venv/bin/python benchmarks/rung4_linkback.py \
        --results benchmarks/rung4-results-sqlite-probed.json \
        --src <sqlite-src> --out linkback-report.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rung4_c_to_rust import SCALAR_RE

# Audit-driven: never link state-dependent "equivalences".
DO_NOT_LINK = {
    "sqlite3.sqlite3HeapNearlyFull": "state-dependent (reads mem0; rung-4 audit)",
}

CFLAGS = [
    "-O1",
    "-w",
    "-DSQLITE_PRIVATE=",
    "-DSQLITE_ENABLE_FTS3",
    "-DSQLITE_ENABLE_FTS5",
    "-DSQLITE_ENABLE_RTREE",
]

SQL_BATTERY = r"""
.echo off
CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, val REAL, big INTEGER);
WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<500)
  INSERT INTO t SELECT x, 'row_'||x||'$id', x*1.5, x*1099511627776 FROM c;
SELECT count(*), sum(val), min(big), max(big) FROM t;
SELECT name FROM t WHERE name LIKE 'row_1%$id' ORDER BY id LIMIT 5;
SELECT abs(-2147483648), hex(1234567890), typeof(9223372036854775807);
SELECT CAST(9.9e14 AS INTEGER), CAST(-9.9e14 AS INTEGER), CAST(1.5 AS INTEGER);
SELECT big = 9.0e18, big < 9.2e18, big > -9.2e18 FROM t WHERE id IN (1, 250, 500);
SELECT id FROM t WHERE val BETWEEN 100.5 AND 110.5 ORDER BY val DESC;
CREATE INDEX idx_val ON t(val);
EXPLAIN QUERY PLAN SELECT * FROM t WHERE val > 250.0 ORDER BY val;
CREATE VIRTUAL TABLE ft USING fts5(body);
INSERT INTO ft SELECT 'the quick brown fox jumps over row '||x FROM (
  WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<200) SELECT x FROM c);
SELECT count(*) FROM ft WHERE ft MATCH 'quick AND fox';
SELECT count(*) FROM ft WHERE ft MATCH 'row';
SELECT "quoted$ident", [bracket$ident] FROM (SELECT 1 AS "quoted$ident", 2 AS [bracket$ident]);
SELECT sqlite_version() IS NOT NULL, sqlite_source_id() IS NOT NULL;
PRAGMA integrity_check;
SELECT round(sum(length(hex(big))), 2) FROM t;
DROP TABLE t;
PRAGMA integrity_check;
"""


def winning_functions(results: Path) -> tuple[dict[str, str], list[tuple[str, str]]]:
    data = json.loads(results.read_text())
    winners: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for r in data["results"]:
        if not r["solved_by"]:
            continue
        if r["component_id"] in DO_NOT_LINK:
            skipped.append((r["component_id"], DO_NOT_LINK[r["component_id"]]))
            continue
        name = r["component_id"].rsplit(".", 1)[-1]
        winners[name] = next(a["candidate"] for a in r["attempts"] if a["stage"] == "ok")
    return winners, skipped


def build_rust_staticlib(winners: dict[str, str], workdir: Path) -> Path:
    lib_rs = workdir / "lib.rs"
    lib_rs.write_text("\n\n".join(winners[n] for n in sorted(winners)) + "\n")
    out = workdir / "libcgir_rewrites.a"
    subprocess.run(
        ["rustc", "--crate-type=staticlib", "-O", "-o", str(out), str(lib_rs)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return out


def patch_amalgamation(src: Path, names: list[str], workdir: Path) -> Path:
    text = (src / "sqlite3.c").read_text()
    for name in names:
        # external linkage everywhere (decls + defs)
        text = re.sub(
            rf"\bstatic\s+((?:SQLITE_NOINLINE\s+)?(?:const\s+)?{SCALAR_RE}\s+{name}\s*\()",
            r"\1",
            text,
        )
        # Rename the C definition; call sites keep resolving `name` -> Rust.
        # A prototype is emitted at the definition site because plain-static
        # functions had no other declaration.
        pattern = re.compile(
            rf"\b((?:SQLITE_PRIVATE\s+|SQLITE_API\s+|SQLITE_NOINLINE\s+)*)"
            rf"({SCALAR_RE})\s+{name}\s*(\([^)]*\))(\s*\{{)"
        )

        def _rename(m: re.Match[str], _name: str = name) -> str:
            proto = f"{m.group(2)} {_name}{m.group(3)};\n"
            return f"{proto}{m.group(1)}{m.group(2)} {_name}__cgir_replaced{m.group(3)}{m.group(4)}"

        text, n_defs = pattern.subn(_rename, text)
        if n_defs != 1:
            raise SystemExit(f"{name}: expected exactly 1 definition, patched {n_defs}")
    patched = workdir / "sqlite3_linked.c"
    patched.write_text(text)
    return patched


def build_shell(src: Path, amalg: Path, workdir: Path, tag: str, rust_lib: Path | None) -> Path:
    out = workdir / f"sqlite3_{tag}"
    cmd = ["cc", *CFLAGS, str(src / "shell.c"), str(amalg), "-o", str(out), "-lm", "-lpthread"]
    if rust_lib is not None:
        cmd.append(str(rust_lib))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise SystemExit(f"link {tag} failed:\n{proc.stderr[-3000:]}")
    return out


def run_battery(shell: Path, workdir: Path, tag: str) -> str:
    proc = subprocess.run(
        [str(shell), str(workdir / f"battery_{tag}.db")],
        input=SQL_BATTERY,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="cgir-linkback-"))
    winners, skipped = winning_functions(args.results)
    print(f"linking {len(winners)} Rust functions (skipped {len(skipped)}: {skipped})")

    rust_lib = build_rust_staticlib(winners, workdir)
    patched = patch_amalgamation(args.src, sorted(winners), workdir)
    linked = build_shell(args.src, patched, workdir, "rust_inside", rust_lib)
    stock = build_shell(args.src, args.src / "sqlite3.c", workdir, "stock", None)

    # proof the symbols come from Rust: C definitions were renamed away
    nm = subprocess.run(["nm", str(linked)], capture_output=True, text=True).stdout
    from_rust = [n for n in sorted(winners) if re.search(rf"T _?{n}\b", nm)]
    replaced = [n for n in sorted(winners) if f"{n}__cgir_replaced" in nm]

    out_stock = run_battery(stock, workdir, "stock")
    out_linked = run_battery(linked, workdir, "linked")
    identical = out_stock == out_linked

    report = {
        "linked_functions": sorted(winners),
        "skipped": skipped,
        "symbols_provided_by_rust": len(from_rust),
        "c_definitions_renamed": len(replaced),
        "battery_lines": len(out_stock.splitlines()),
        "battery_identical": identical,
        "workdir": str(workdir),
    }
    if not identical:
        import difflib

        delta = list(
            difflib.unified_diff(out_stock.splitlines(), out_linked.splitlines(), lineterm="")
        )[:60]
        report["diff"] = delta
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "linked_functions"}, indent=2))
    print("BATTERY IDENTICAL" if identical else "BATTERY DIVERGED", flush=True)


if __name__ == "__main__":
    main()
