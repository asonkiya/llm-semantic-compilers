"""verify-diff for JS/TS: the Node-backed differential core.

Mirrors the Python verify-diff contract on the JavaScript side — preserving /
diverged (with counterexample) / unverified — including value semantics (float
tolerance, NaN, arrays/objects), exception equivalence, and TypeScript source
(transpiled via the repo's typescript when present). Gated on `node` being
installed; skips cleanly otherwise.
"""

from __future__ import annotations

import pytest

from cgir.js_verify import differential_replay_js, node_available
from cgir.verify_diff import DIVERGED, PRESERVING, UNVERIFIED

pytestmark = pytest.mark.skipif(not node_available(), reason="node not on PATH")


def _run(tmp_path, name, old, new, args, is_ts=False):
    return differential_replay_js(tmp_path, f"m:{name}", name, old, new, args, is_ts=is_ts)


def test_preserving_when_equivalent(tmp_path):
    old = "function tax(p, r) { return p * (1 + r); }"
    new = "function tax(p, r) { return p + p * r; }"  # algebraically equal
    v = _run(tmp_path, "tax", old, new, [[100, 0.2], [0, 0.5], [50, 0]])
    assert v.status == PRESERVING and v.checked == 3


def test_diverged_gives_counterexample(tmp_path):
    old = "function tax(p, r) { return p * (1 + r); }"
    new = "function tax(p, r) { return p + (1 + r); }"  # the '* -> +' typo
    v = _run(tmp_path, "tax", old, new, [[100, 0.2]])
    assert v.status == DIVERGED
    assert "[100,0.2]" in v.detail and "120" in v.detail and "101.2" in v.detail


def test_arrow_and_const_form_reconstructs(tmp_path):
    old = "const dbl = (x) => x * 2;"
    new = "const dbl = (x) => x + x;"
    assert _run(tmp_path, "dbl", old, new, [[3], [10], [0]]).status == PRESERVING


def test_object_return_deep_compared(tmp_path):
    old = "function mk(a, b) { return { sum: a + b, items: [a, b] }; }"
    new = "function mk(a, b) { return { sum: b + a, items: [a, b] }; }"
    assert _run(tmp_path, "mk", old, new, [[1, 2], [5, 7]]).status == PRESERVING
    bad = "function mk(a, b) { return { sum: a + b, items: [b, a] }; }"  # order flipped
    assert _run(tmp_path, "mk", old, bad, [[1, 2]]).status == DIVERGED


def test_matching_exceptions_are_preserving(tmp_path):
    old = "function f(x) { if (!x) throw new RangeError('z'); return 1 / x; }"
    new = "function f(x) { if (x === 0) throw new RangeError('z'); return 1 / x; }"
    v = _run(tmp_path, "f", old, new, [[2], [0]])
    assert v.status == PRESERVING


def test_new_throws_where_old_returned_is_divergence(tmp_path):
    old = "function f(x) { return x || 0; }"
    new = "function f(x) { if (!x) throw new Error('boom'); return x; }"
    v = _run(tmp_path, "f", old, new, [[0]])
    assert v.status == DIVERGED and "Error" in v.detail


def test_float_tolerance_matches_python(tmp_path):
    # 0.1 + 0.2 vs 0.3 — within tolerance, must be preserving not a false divergence
    old = "function f() { return 0.3; }"
    new = "function f() { return 0.1 + 0.2; }"
    assert _run(tmp_path, "f", old, new, [[]]).status == PRESERVING


def test_no_inputs_is_unverified(tmp_path):
    old = new = "function f(x) { return x; }"
    v = _run(tmp_path, "f", old, new, [])
    assert v.status == UNVERIFIED and "not exercised" in v.detail


def test_broken_new_source_is_unverified_not_crash(tmp_path):
    old = "function f(x) { return x; }"
    new = "function f(x) { return x +++ ; }"  # syntax error
    v = _run(tmp_path, "f", old, new, [[1]])
    assert v.status == UNVERIFIED and "new version" in v.detail


def test_commonjs_module_exports_resolves(tmp_path):
    old = "function area(w, h) { return w * h; }\nmodule.exports = { area };"
    new = "function area(w, h) { return h * w; }\nmodule.exports = { area };"
    assert _run(tmp_path, "area", old, new, [[3, 4]]).status == PRESERVING


def test_module_level_dependency_resolves_not_false_preserving(tmp_path):
    """The bug the real-repo pass caught: a function that references a
    module-level binding must be evaluated in the WHOLE module, or both versions
    throw ReferenceError identically and the gate falsely reports 'preserving'.
    Here old and new genuinely differ and must diverge."""
    old = "const K = 1024;\nfunction f(n) { return n * K; }\nmodule.exports = { f };"
    new = "const K = 1024;\nfunction f(n) { return n * K + 1; }\nmodule.exports = { f };"
    v = _run(tmp_path, "f", old, new, [[2]])
    assert v.status == DIVERGED and "2048" in v.detail and "2049" in v.detail


def test_esm_without_esbuild_is_unverified_not_false_pass(tmp_path):
    old = "export function area(w, h) { return w * h; }"
    new = "export function area(w, h) { return h * w; }"
    v = _run(tmp_path, "area", old, new, [[3, 4]])
    # our test repo has no esbuild — honest 'unverified', never a silent pass
    assert v.status == UNVERIFIED and "esbuild" in v.detail


_TS_PROBE = (
    "let ok=false;"
    "try{require('esbuild');ok=true}catch(e){}"
    "try{const t=require('typescript');ok=ok||typeof t.transpileModule==='function'}catch(e){}"
    "process.exit(ok?0:1)"
)


@pytest.mark.skipif(
    __import__("subprocess").run(["node", "-e", _TS_PROBE], capture_output=True).returncode != 0,
    reason="no resolvable TS transpiler (esbuild or typescript@5)",
)
def test_typescript_source_transpiles_and_compares(tmp_path):
    old = "function add(a: number, b: number): number { return a + b; }"
    new = "function add(a: number, b: number): number { return b + a; }"
    v = differential_replay_js(tmp_path, "m:add", "add", old, new, [[2, 3]], is_ts=True)
    assert v.status == PRESERVING


# --- the git + tree-sitter layer + end to end -------------------------------

import subprocess as _sp  # noqa: E402

from cgir.js_verify import changed_js_functions, verify_diff_js  # noqa: E402


def _init_git(d):
    _sp.run(["git", "-C", str(d), "init", "-q"], check=True)
    _sp.run(["git", "-C", str(d), "add", "-A"], check=True)
    _sp.run(
        [
            "git",
            "-C",
            str(d),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "b",
        ],
        check=True,
    )


def test_changed_js_functions_detects_edit(tmp_path):
    (tmp_path / "m.js").write_text(
        "function tax(p, r) { return p * (1 + r); }\n"
        "const dbl = (x) => x * 2;\n"
        "function untouched(x) { return x; }\n"
    )
    _init_git(tmp_path)
    (tmp_path / "m.js").write_text(
        "function tax(p, r) { return p + (1 + r); }\n"  # changed
        "const dbl = (x) => x + x; // changed too\n"
        "function untouched(x) { return x; }\n"
    )
    changed, _notes = changed_js_functions(tmp_path, "HEAD")
    names = {c.func_name for c in changed}
    assert names == {"tax", "dbl"}
    tax = next(c for c in changed if c.func_name == "tax")
    assert "p * (1 + r)" in tax.old_src and "p + (1 + r)" in tax.new_src and not tax.is_ts


def test_changed_ts_functions_flagged_as_ts(tmp_path):
    (tmp_path / "m.ts").write_text("export function id(x: number): number { return x; }\n")
    _init_git(tmp_path)
    (tmp_path / "m.ts").write_text("export function id(x: number): number { return x + 0; }\n")
    changed, _ = changed_js_functions(tmp_path, "HEAD")
    assert len(changed) == 1 and changed[0].is_ts


def test_changed_js_bad_ref_is_note(tmp_path):
    (tmp_path / "m.js").write_text("function f(){return 1;}\n")
    _init_git(tmp_path)
    changed, notes = changed_js_functions(tmp_path, "no-such-ref")
    assert changed == [] and any("valid ref" in n for n in notes)


def _has_node_test():
    return (
        _sp.run(
            ["node", "--test", "--test-name-pattern=nope"], capture_output=True, cwd="/tmp"
        ).returncode
        is not None
    )


def test_verify_diff_js_end_to_end(tmp_path):
    """Record real inputs via `node --test`, then flag a regression + pass a
    behavior-preserving refactor, in one run."""
    (tmp_path / "shop.js").write_text(
        "function tax(p, r) { return p * (1 + r); }\n"
        "function round2(x) { return Math.round(x * 100) / 100; }\n"
        "module.exports = { tax, round2 };\n"
    )
    (tmp_path / "shop.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "const { tax, round2 } = require('./shop.js');\n"
        "test('tax', () => { assert.strictEqual(tax(100, 0.2), 120); assert.strictEqual(tax(50, 0), 50); });\n"
        "test('round2', () => { assert.strictEqual(round2(1.005), 1); });\n"
    )
    _init_git(tmp_path)
    # break tax (* -> +), refactor round2 equivalently
    (tmp_path / "shop.js").write_text(
        "function tax(p, r) { return p + (1 + r); }\n"  # BROKEN
        "function round2(x) { const s = x * 100; return Math.round(s) / 100; }\n"  # same
        "module.exports = { tax, round2 };\n"
    )
    verdicts = verify_diff_js(tmp_path, "HEAD")
    by = {v.qualname.split(":")[-1]: v for v in verdicts}
    assert by["tax"].status == DIVERGED, by["tax"]
    assert "120" in by["tax"].detail
    assert by["round2"].status == PRESERVING, by["round2"]
