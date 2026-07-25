"""The C lifter (:mod:`cgir.ffi.sources.c_lift`): pull a function + its
file-scope dependencies out of a larger source into a standalone compilable TU.

The unlock is a kernel function that reads a file-scope *table* (the AES S-box):
it can't be lifted alone, but ``lift_symbol`` tree-shakes the table in with it so
the C->Rust pipeline gets a compilable ``--c-source``. The regression guard here
is the one that mattered: ``extract_definition`` must match only *file-scope*
definitions, never a local variable inside a function body (which, hoisted to
file scope, references parameters that aren't in scope there).
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from cgir.ffi.sources import c_lift
from cgir.ffi.sources.c_lift import (
    DEFAULT_SHIM,
    build_def_index,
    extract_definition,
    lift_symbol,
    lift_symbols,
)

# A miniature "amalgamation": a table, a #define, a helper, and the target that
# reads all three — plus a decoy function whose body declares locals named like
# our file-scope symbols (the trap the file-scope guard must not fall into).
_SRC = """\
#define ROUNDS 4

static const unsigned char sbox[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };

static unsigned mix(unsigned a) {
    return a ^ (a << 1);
}

static void decoy(unsigned q) {
    unsigned sbox = q + 1;     /* a LOCAL named like the table */
    unsigned target = sbox * 2;
    (void)target;
}

unsigned target(unsigned in) {
    unsigned acc = 0;
    for (unsigned r = 0; r < ROUNDS; r++)
        acc ^= mix(sbox[in & 7]);
    return acc;
}
"""


def test_extract_define():
    d = extract_definition("ROUNDS", _SRC)
    assert d is not None and d.strip() == "#define ROUNDS 4"


def test_extract_table_brace_matched():
    d = extract_definition("sbox", _SRC)
    assert d is not None
    assert d.startswith("static const unsigned char sbox[8] =")
    assert d.rstrip().endswith("};")
    # the whole initializer, not a truncation at the first value
    assert "8 }" in d


def test_extract_function_brace_matched():
    d = extract_definition("mix", _SRC)
    assert d is not None
    assert d.startswith("static unsigned mix(unsigned a)")
    assert d.count("{") == d.count("}") == 1


def test_extract_ignores_local_variable_shadowing_a_name():
    """`sbox` and `target` also appear as *locals* inside `decoy`. Extraction
    must return the file-scope definitions, never the local declarations."""
    # sbox resolves to the file-scope table (has an array initializer), not
    # `unsigned sbox = q + 1;` inside decoy.
    assert "= q + 1" not in (extract_definition("sbox", _SRC) or "")
    # `target` resolves to the file-scope function, not `unsigned target = ...`.
    d = extract_definition("target", _SRC)
    assert d is not None and d.startswith("unsigned target(unsigned in)")


def test_extract_absent_symbol_is_none():
    assert extract_definition("nonexistent", _SRC) is None


# A function-like macro immediately followed by a real function — the tiny-AES-c
# idiom (`#define getSBoxValue(num) (sbox[(num)])` then `KeyExpansion`). The
# macro's param list must not run on into the next function's `(...) {`.
_MACRO_THEN_FN = """\
static const unsigned char tbl[4] = { 9, 8, 7, 6 };

#define lookup(i) (tbl[(i)])

/* a comment between them */
static unsigned consume(unsigned n) {
    unsigned s = 0;
    for (unsigned i = 0; i < n; i++) s += lookup(i);
    return s;
}
"""


def test_extract_function_like_macro_does_not_overgrab_next_function():
    d = extract_definition("lookup", _MACRO_THEN_FN)
    assert d is not None
    assert d.strip() == "#define lookup(i) (tbl[(i)])"  # exactly the macro, one line
    assert "consume" not in d  # not the adjacent function


def test_extract_prefers_real_function_over_a_commented_out_one():
    """A commented-out function beside its macro: the macro is the definition."""
    src = (
        "#define getval(x) ((x) + 1)\n"
        "/*\nstatic int getval(int x) { return x + 1; }\n*/\n"
        "int use(int y) { return getval(y); }\n"
    )
    d = extract_definition("getval", src)
    assert d is not None and d.strip() == "#define getval(x) ((x) + 1)"


def test_lift_macro_pulls_only_its_real_data_deps():
    """Lifting the macro pulls the table it reads, not the unrelated function
    that happens to sit next to it in the file."""
    tu, _ = lift_symbol(_MACRO_THEN_FN, "lookup")
    assert tu is not None
    assert "tbl[4] =" in tu  # the table the macro indexes
    assert "consume" not in tu  # the adjacent function is not a dependency


# An unbalanced brace hidden in a char/string literal *before* the target — this
# is what defeats a naive global brace-count on a real amalgamation (SQLite has a
# handful of literal braces). File-scope is decided by column-0, not by counting,
# so the table and its reader are still found.
_BRACE_TRAP = r"""
static int brace_char = '{';        /* a lone '{' in a char literal */
static const char *j = "unbalanced } brace in a string {{{";

static const int sizes[4] = { 10, 20, 30, 40 };

static int size_of(int i) {
    int local_arr[2] = { 1, 2 };    /* a local, indented array — must NOT lift */
    return sizes[i] + local_arr[0];
}
"""


def test_file_scope_survives_unbalanced_braces_in_literals():
    d = extract_definition("size_of", _BRACE_TRAP)
    assert d is not None and d.startswith("static int size_of(int i)")
    tu, _ = lift_symbol(_BRACE_TRAP, "size_of")
    assert tu is not None
    assert "sizes[4] =" in tu  # the file-scope table is pulled despite the trap


def test_local_indented_array_is_not_hoisted():
    """`local_arr` is an indented array inside a body — extracting it must fail
    (it is not a file-scope definition), even though its shape matches a table."""
    assert extract_definition("local_arr", _BRACE_TRAP) is None


# The SQLite pattern: the file typedefs its own scalar spellings, so a lift
# with a minimal shim must pull the typedef chain rather than fail on `u8`.
_TYPEDEF_SRC = """\
#define UINT8_TYPE unsigned char
typedef UINT8_TYPE u8;
typedef struct Pair { int a; int b; } Pair;

static const u8 lut[3] = { 5, 6, 7 };

u8 pick(u8 i) {
    return lut[i % 3];
}

int pair_sum_kind(int a, int b) {
    Pair p = { a, b };
    return p.a + p.b;
}
"""


def test_extract_simple_typedef():
    assert extract_definition("u8", _TYPEDEF_SRC) == "typedef UINT8_TYPE u8;"


def test_extract_struct_body_typedef():
    d = extract_definition("Pair", _TYPEDEF_SRC)
    assert d is not None and d.startswith("typedef struct Pair") and d.endswith("Pair;")


def test_lift_pulls_typedef_chain():
    """`pick` needs `u8` → `UINT8_TYPE`: both are pulled, so a minimal shim
    (no scalar typedefs) still yields a compilable TU."""
    tu, unresolved = lift_symbol(_TYPEDEF_SRC, "pick")
    assert tu is not None
    assert "typedef UINT8_TYPE u8;" in tu
    assert "#define UINT8_TYPE unsigned char" in tu
    assert not ({"u8", "UINT8_TYPE", "lut"} & set(unresolved))


def test_lift_symbols_merges_closures_into_one_tu():
    tu, _, missing = lift_symbols(_TYPEDEF_SRC, ["pick", "pair_sum_kind"])
    assert missing == []
    assert tu is not None
    assert tu.count("typedef UINT8_TYPE u8;") == 1  # shared deps deduped
    assert "pick" in tu and "pair_sum_kind" in tu and "Pair" in tu


def test_lift_symbols_reports_missing():
    tu, _, missing = lift_symbols(_TYPEDEF_SRC, ["pick", "no_such_fn"])
    assert tu is not None  # the found symbol still lifts
    assert missing == ["no_such_fn"]
    tu2, _, missing2 = lift_symbols(_TYPEDEF_SRC, ["nope"])
    assert tu2 is None and missing2 == ["nope"]


# `#if defined(X)` at column 0, with a brace-opening line after it — the SQLite
# countLeadingZeros regression: `defined(...)` must never read as a function
# definition (it pulled a 300-line block of unrelated global state).
_PREPROC_TRAP = """\
#if defined(SOME_FLAG)
static struct Big { int x; } huge_state =
{ 42 };
#endif

static int clz8(unsigned v) {
#if defined(__GNUC__) \\
    && !defined(NO_INTRINSIC)
  return v ? __builtin_clz(v) - 24 : 8;
#else
  int n = 0;
  while( !(v & 0x80) && n < 8 ){ n++; v <<= 1; }
  return n;
#endif
}
"""


def test_preprocessor_directive_is_never_a_definition_site():
    assert extract_definition("defined", _PREPROC_TRAP) is None


def test_lift_function_with_preprocessor_conditionals():
    """A function whose body has `#if defined(...)` branches lifts alone — the
    directive's identifiers don't drag in unrelated file-scope state."""
    tu, unresolved = lift_symbol(_PREPROC_TRAP, "clz8")
    assert tu is not None
    assert "huge_state" not in tu  # the unrelated #if block was not pulled
    assert "defined" not in unresolved  # preprocessor operator, not a dependency


def test_default_shim_defines_no_functions():
    """The shim provides typedefs/macros only — an unresolved *call* must fail
    the compile rather than be silently satisfied."""
    assert "(" not in DEFAULT_SHIM.replace("(x)", "").replace("(void*)0", "")
    assert "typedef" in DEFAULT_SHIM


def test_cli_lift_dry_run(tmp_path):
    """`cgir rewrite --lang c-rust --c-source big.c --lift fn` — one command:
    lift, index the lifted TU, and show it as regenerable. No pre-existing
    index needed."""
    from typer.testing import CliRunner

    from cgir.cli import app

    big = tmp_path / "big.c"
    big.write_text(_TYPEDEF_SRC)
    out_dir = tmp_path / "lifted"
    result = CliRunner().invoke(
        app,
        [
            "rewrite",
            "--lang",
            "c-rust",
            "--c-source",
            str(big),
            "--lift",
            "pair_sum_kind",
            "--lift-out",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "lifted pair_sum_kind" in result.output
    assert "pair_sum_kind" in result.output and "regenerable" in result.output
    assert (out_dir / "lifted_big.c").exists()


def test_cli_lift_unknown_symbol_fails_cleanly(tmp_path):
    from typer.testing import CliRunner

    from cgir.cli import app

    big = tmp_path / "big.c"
    big.write_text(_TYPEDEF_SRC)
    result = CliRunner().invoke(
        app,
        ["rewrite", "--lang", "c-rust", "--c-source", str(big), "--lift", "ghost"],
    )
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_lift_pulls_transitive_file_scope_deps():
    tu, unresolved = lift_symbol(_SRC, "target")
    assert tu is not None
    # target transitively needs ROUNDS, sbox, and mix — all pulled in.
    for dep in ("#define ROUNDS 4", "sbox[8] =", "mix(unsigned a)"):
        assert dep in tu
    # the decoy (not referenced) is NOT pulled.
    assert "decoy" not in tu
    # locals/params/comment-words are reported unresolved, never mis-defined.
    assert "in" in unresolved  # target's own parameter


def test_lift_absent_symbol_returns_none():
    tu, unresolved = lift_symbol(_SRC, "nope")
    assert tu is None and unresolved == []


def test_lift_prepends_shim():
    tu, _ = lift_symbol(_SRC, "mix", shim="typedef unsigned u32;")
    assert tu is not None and tu.startswith("typedef unsigned u32;")


def test_build_def_index_matches_per_name_extraction():
    """The one-pass index must answer every lookup exactly as a fresh per-name
    ``extract_definition`` would — it's the same patterns with the loop inverted.
    (Equivalence was validated over 500+ names of the 9.5 MB SQLite amalgamation;
    this pins it on the in-repo fixtures across every definition category.)"""
    for src in (_SRC, _MACRO_THEN_FN, _TYPEDEF_SRC, _BRACE_TRAP, _PREPROC_TRAP):
        index = build_def_index(src)
        names = set(re.findall(r"\b([A-Za-z_]\w*)\b", src))
        for name in names:
            # a lone call (builds its own index) and an index-backed call agree
            assert (
                extract_definition(name, src)
                == index.get(name)
                == (extract_definition(name, src, index=index))
            ), name


def test_extract_definition_accepts_shared_index():
    """Callers doing many lookups pass a prebuilt index; results are identical to
    the un-indexed call (this is the O(1) fast path the sweep relies on)."""
    index = build_def_index(_TYPEDEF_SRC)
    assert extract_definition("pick", _TYPEDEF_SRC, index=index).startswith("u8 pick(")
    assert extract_definition("no_such", _TYPEDEF_SRC, index=index) is None


def test_lift_runaway_closure_is_capped(monkeypatch):
    """A function reaching a huge in-file struct/helper graph (SQLite's `Parse`)
    must not scan the whole symbol table — past ``MAX_CLOSURE`` the lift bails to
    None instead of running away. Simulated with a long dependency chain and a
    tiny cap."""
    # f0 -> f1 -> ... -> fN, each calling the next: a closure of N+1 defs.
    n = 30
    parts = [f"int f{i}(int x) {{ return f{i + 1}(x); }}" for i in range(n)]
    parts.append(f"int f{n}(int x) {{ return x; }}")
    src = "\n".join(parts) + "\n"
    # generous cap: the whole chain lifts.
    assert lift_symbol(src, "f0")[0] is not None
    # tight cap: the closure blows past it -> not liftable.
    monkeypatch.setattr(c_lift, "MAX_CLOSURE", 5)
    assert lift_symbol(src, "f0")[0] is None


def test_lift_struct_function_resolves_field_names_to_unresolved():
    """The perf-bug shape: a struct-pointer function references struct *field*
    names and *locals* that have no file-scope definition. They must resolve to
    unresolved (never mis-defined), while the struct typedef itself is pulled —
    and the whole lift is fast (O(1) lookups, no per-field full-file scan)."""
    src = """\
typedef struct Node { int lo; int hi; struct Node *next; } Node;

int span(Node *p) {
    int lo = p->lo;
    int hi = p->hi;
    return hi - lo;
}
"""
    tu, unresolved = lift_symbol(src, "span")
    assert tu is not None
    assert "typedef struct Node" in tu  # the struct layout is pulled
    # field/local names are reported unresolved, not turned into bogus file-scope defs
    assert {"lo", "hi"} <= set(unresolved)
    assert "int lo =" not in tu.split("int span")[0]  # no local hoisted above the fn


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_lifted_tu_compiles_standalone(tmp_path):
    """The point of the whole module: the lifted TU is self-contained and
    compiles, where the target function alone would not (undefined `sbox`)."""
    tu, unresolved = lift_symbol(_SRC, "target")
    assert tu is not None
    # nothing file-scope is left unresolved (the deps were all pulled in);
    # what remains is only locals/params/comment tokens, which don't block a build.
    assert not ({"ROUNDS", "sbox", "mix"} & set(unresolved))
    c = tmp_path / "lifted.c"
    c.write_text(tu)
    r = subprocess.run(
        ["cc", "-c", "-w", str(c), "-o", str(tmp_path / "lifted.o")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# Header-aware lifting: the kernel's definitions live in HEADERS (struct
# layouts, static-inline helpers, macros), so a single-file tree-shake can't
# reach them. resolve_includes() walks the #include closure, build_multi_index
# unions per-file def-indexes (the .c wins name clashes), and lift emits pulled
# defs in dependency order (headers before their includers). The compile check
# stays the arbiter of every pull.
# ---------------------------------------------------------------------------


def _header_tree(tmp_path):
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "types.h").write_text("typedef unsigned char myu8;\n")
    (inc / "tab.h").write_text(
        '#include "types.h"\n'
        "static const myu8 TAB[3] = { 1, 2, 3 };\n"
        "static inline myu8 pick2(myu8 i) { return TAB[i % 3]; }\n"
    )
    (inc / "loop_a.h").write_text('#include "loop_b.h"\ntypedef int aa_t;\n')
    (inc / "loop_b.h").write_text('#include "loop_a.h"\ntypedef int bb_t;\n')
    c = tmp_path / "main.c"
    c.write_text(
        "#include <tab.h>\n"
        '#include "gone_missing.h"\n'
        "\n"
        "int use(int i) {\n"
        "  return pick2((myu8)i) + 1;\n"
        "}\n"
    )
    return c, inc


def test_resolve_includes_walks_closure_in_dependency_order(tmp_path):
    from cgir.ffi.sources.c_lift import resolve_includes

    c, inc = _header_tree(tmp_path)
    files = resolve_includes(c, [inc])
    names = [f.name for f in files]
    # post-order: a header lands before every file that includes it; the .c is last
    assert names[-1] == "main.c"
    assert names.index("types.h") < names.index("tab.h") < names.index("main.c")
    # the missing include is skipped, not fatal
    assert "gone_missing.h" not in names


def test_resolve_includes_survives_cycles(tmp_path):
    from cgir.ffi.sources.c_lift import resolve_includes

    _, inc = _header_tree(tmp_path)
    a = inc / "loop_a.h"
    files = resolve_includes(a, [inc])
    assert [f.name for f in files].count("loop_a.h") == 1  # visited once, no recursion blowup


def test_multi_index_main_file_wins_name_clash(tmp_path):
    from cgir.ffi.sources.c_lift import build_multi_index, resolve_includes

    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "dup.h").write_text("static inline int both(int x) { return x + 100; }\n")
    c = tmp_path / "main.c"
    c.write_text('#include "include/dup.h"\nstatic int both(int x) { return x + 1; }\n')
    index, ranks = build_multi_index(resolve_includes(c, [inc]))
    assert "+ 1" in index["both"] and "+ 100" not in index["both"]
    assert "both" in ranks


def test_lift_pulls_struct_and_inline_helper_from_header(tmp_path):
    """The kernel shape: the target's table, helper, and typedef chain live in
    headers. Header-aware lift pulls all of them into one compilable TU;
    single-file lift (the old behavior, still the default) leaves them
    unresolved."""
    from cgir.ffi.sources.c_lift import lift_symbols_from_file

    c, inc = _header_tree(tmp_path)
    # old behavior preserved: no include dirs -> helper unresolved
    tu0, unresolved0, _ = lift_symbols_from_file(c, ["use"])
    assert tu0 is not None and "pick2" in unresolved0
    # header-aware: everything pulled, dependency-ordered, compilable
    tu, unresolved, missing = lift_symbols_from_file(c, ["use"], include_dirs=[inc])
    assert missing == []
    assert tu is not None
    assert "typedef unsigned char myu8;" in tu
    assert "TAB[3]" in tu and "pick2" in tu
    assert not ({"pick2", "TAB", "myu8"} & set(unresolved))
    # dependency order: typedef before table before helper before target
    assert tu.index("typedef unsigned char myu8") < tu.index("TAB[3]") < tu.index("int use")


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_header_aware_lifted_tu_compiles(tmp_path):
    from cgir.ffi.sources.c_lift import lift_symbols_from_file

    c, inc = _header_tree(tmp_path)
    tu, _, _ = lift_symbols_from_file(c, ["use"], include_dirs=[inc])
    assert tu is not None
    out = tmp_path / "lifted.c"
    out.write_text(tu)
    r = subprocess.run(
        ["cc", "-c", "-w", str(out), "-o", str(tmp_path / "lifted.o")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_header_cache_is_shared_across_lifts(tmp_path):
    """A sweep over many files of one tree indexes each header once — the
    cache maps path -> per-file index and is filled on first use."""
    from cgir.ffi.sources.c_lift import lift_symbols_from_file

    c, inc = _header_tree(tmp_path)
    cache: dict = {}
    lift_symbols_from_file(c, ["use"], include_dirs=[inc], header_cache=cache)
    assert any("tab.h" in k for k in cache)
    n = len(cache)
    lift_symbols_from_file(c, ["use"], include_dirs=[inc], header_cache=cache)
    assert len(cache) == n  # second lift reused every entry


# Kernel house style: PLAIN `struct Name { ... };` and `enum { ... };` — no
# typedef. The first header-aware kernel sweep showed the index blind to these
# (shash_desc unresolved 133 times while defined in the pulled headers).
_KERNEL_STYLE = """\
struct sctx {
    unsigned int state[8];
    unsigned long count;
} __attribute__((aligned(8)));

enum flags {
    FL_INIT = 1,
    FL_FINAL = 4,
};

enum { ANON_A = 10, ANON_B };

static inline void ctx_reset(struct sctx *c) {
    c->count = FL_INIT + ANON_A;
}

int step(struct sctx *c) {
    ctx_reset(c);
    return (int)c->count + FL_FINAL;
}
"""


def test_extract_plain_struct_definition():
    d = extract_definition("sctx", _KERNEL_STYLE)
    assert d is not None and d.startswith("struct sctx {") and "count" in d
    assert d.rstrip().endswith(";")  # through the attribute tail to the semicolon


def test_extract_enum_by_tag_and_by_enumerator():
    by_tag = extract_definition("flags", _KERNEL_STYLE)
    assert by_tag is not None and "FL_FINAL" in by_tag
    # enumerators resolve to their whole enum definition — named and anonymous
    assert extract_definition("FL_INIT", _KERNEL_STYLE) == by_tag
    anon = extract_definition("ANON_A", _KERNEL_STYLE)
    assert anon is not None and anon.startswith("enum {") and "ANON_B" in anon


def test_lift_kernel_style_function_pulls_struct_enum_helper():
    tu, unresolved, missing = lift_symbols(_KERNEL_STYLE, ["step"])
    assert missing == []
    assert tu is not None
    for dep in ("struct sctx {", "enum flags {", "ANON_A", "ctx_reset"):
        assert dep in tu, dep
    assert not ({"sctx", "flags", "FL_INIT", "FL_FINAL", "ANON_A", "ctx_reset"} & set(unresolved))


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_lifted_kernel_style_tu_compiles(tmp_path):
    tu, _, _ = lift_symbols(_KERNEL_STYLE, ["step"])
    assert tu is not None
    c = tmp_path / "k.c"
    c.write_text(tu)
    r = subprocess.run(
        ["cc", "-c", "-w", str(c), "-o", str(tmp_path / "k.o")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
