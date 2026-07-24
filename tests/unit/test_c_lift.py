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

import shutil
import subprocess

import pytest

from cgir.ffi.sources.c_lift import extract_definition, lift_symbol

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
