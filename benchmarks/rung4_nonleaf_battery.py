"""Behavioral battery for the at-scale non-leaf C->Rust sweep.

Takes the `--apply --link-out` artifacts (patched `sqlite3_linked.c` + the
Rust staticlib) that `cgir rewrite --lang c-rust --non-leaf` produced, builds
a *real* sqlite3 shell from them + shell.c, builds a stock shell, and diffs a
SQL battery byte-for-byte. This is the flagship proof: a connected subgraph of
a 150k-LOC C engine rewritten to Rust in one pass and behaviorally
indistinguishable when assembled — including Rust functions that call other
Rust functions.

    python benchmarks/rung4_nonleaf_battery.py --link <link-out-dir> --src <sqlite-src>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

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
SELECT big = 9.0e18, big < 9.2e18 FROM t WHERE id IN (1, 250, 500);
SELECT id FROM t WHERE val BETWEEN 100.5 AND 110.5 ORDER BY val DESC;
CREATE INDEX idx_val ON t(val);
EXPLAIN QUERY PLAN SELECT * FROM t WHERE val > 250.0 ORDER BY val;
CREATE VIRTUAL TABLE ft USING fts5(body);
INSERT INTO ft SELECT 'the quick brown fox jumps over row '||x FROM (
  WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<200) SELECT x FROM c);
SELECT count(*) FROM ft WHERE ft MATCH 'quick AND fox';
SELECT count(*) FROM ft WHERE ft MATCH 'row';
CREATE VIRTUAL TABLE fts3t USING fts3(content);
INSERT INTO fts3t VALUES('consonants and vowels in stemming');
SELECT count(*) FROM fts3t WHERE content MATCH 'stem*';
SELECT "quoted$ident" FROM (SELECT 1 AS "quoted$ident");
SELECT sqlite_version() IS NOT NULL;
PRAGMA integrity_check;
SELECT round(sum(length(hex(big))), 2) FROM t;
DROP TABLE t;
PRAGMA integrity_check;
"""


def build(shell_src: Path, amalg: Path, out: Path, staticlib: Path | None) -> None:
    cmd = ["cc", *CFLAGS, str(shell_src), str(amalg), "-o", str(out), "-lm", "-lpthread"]
    if staticlib is not None:
        force = (
            f"-Wl,-force_load,{staticlib}" if sys.platform == "darwin" else "-Wl,--whole-archive"
        )
        if sys.platform == "darwin":
            cmd.append(force)
        else:
            cmd += ["-Wl,--whole-archive", str(staticlib), "-Wl,--no-whole-archive"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise SystemExit(f"build failed for {out.name}:\n{proc.stderr[-3000:]}")


def run(shell: Path, db: Path) -> str:
    proc = subprocess.run(
        [str(shell), str(db)], input=SQL_BATTERY, capture_output=True, text=True, timeout=300
    )
    return proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", type=Path, required=True, help="--link-out dir")
    ap.add_argument("--src", type=Path, required=True, help="sqlite amalgamation dir")
    args = ap.parse_args()

    patched = next(args.link.glob("*_linked.c"))
    staticlib = next(args.link.glob("*.a"))
    wd = Path(args.link)
    rust_shell = wd / "sqlite3_rust"
    stock_shell = wd / "sqlite3_stock"
    build(args.src / "shell.c", patched, rust_shell, staticlib)
    build(args.src / "shell.c", args.src / "sqlite3.c", stock_shell, None)

    for db in (wd / "rust.db", wd / "stock.db"):
        db.unlink(missing_ok=True)  # fresh DB each run (CREATE VIRTUAL TABLE isn't idempotent)
    rust_out = run(rust_shell, wd / "rust.db")
    stock_out = run(stock_shell, wd / "stock.db")
    identical = rust_out == stock_out
    print(f"battery lines: {len(stock_out.splitlines())}")
    print("BATTERY IDENTICAL" if identical else "BATTERY DIVERGED")
    if not identical:
        import difflib

        for line in list(
            difflib.unified_diff(stock_out.splitlines(), rust_out.splitlines(), lineterm="")
        )[:40]:
            print(line)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
