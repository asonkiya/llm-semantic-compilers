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


# --- harness eq: containers with no own enumerable keys ---------------------


def test_map_return_divergence_detected(tmp_path):
    """Maps have no own enumerable keys, so a keys-only deep-compare calls ANY
    two Maps equal — a changed function returning a different Map verified as
    'preserving'. Must diverge."""
    old = "function f() { return new Map([[1, 2]]); }"
    new = "function f() { return new Map([[1, 3]]); }"
    assert _run(tmp_path, "f", old, new, [[]]).status == DIVERGED


def test_set_and_date_divergence_detected(tmp_path):
    old = "function f() { return new Set([1]); }"
    new = "function f() { return new Set([2, 3]); }"
    assert _run(tmp_path, "f", old, new, [[]]).status == DIVERGED
    old = "function g() { return new Date(0); }"
    new = "function g() { return new Date(1e12); }"
    assert _run(tmp_path, "g", old, new, [[]]).status == DIVERGED


def test_regexp_divergence_detected(tmp_path):
    old = "function f() { return /a+/g; }"
    new = "function f() { return /b+/i; }"
    assert _run(tmp_path, "f", old, new, [[]]).status == DIVERGED


def test_unchanged_containers_preserving(tmp_path):
    old = new = (
        "function f() { return { m: new Map([['k', 1]]), s: new Set([1, 2]),"
        " d: new Date(1000), r: /x/g }; }"
    )
    assert _run(tmp_path, "f", old, new, [[]]).status == PRESERVING


def test_different_class_same_shape_diverges(tmp_path):
    # {} and new Date(0) both have zero own enumerable keys — must not match.
    old = "function f() { return {}; }"
    new = "function f() { return new Date(0); }"
    assert _run(tmp_path, "f", old, new, [[]]).status == DIVERGED


def test_cyclic_structures_terminate(tmp_path):
    old = new = "function f() { const o = { a: 1 }; o.self = o; return o; }"
    assert _run(tmp_path, "f", old, new, [[]]).status == PRESERVING


# --- capture: args must be snapshotted BEFORE the call -----------------------


def test_capture_snapshots_args_before_call(tmp_path):
    """A function that mutates its object argument: serializing args after the
    call records the post-call state as the 'input', so replays run on the
    wrong input. The recorded args must be the pre-call values."""
    (tmp_path / "m.js").write_text(
        "function total(o) { o.items.push(0); return o.items.length; }\n"
        "module.exports = { total };\n"
    )
    (tmp_path / "m.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "const { total } = require('./m.js');\n"
        "test('total', () => { assert.strictEqual(total({ items: [1, 2] }), 3); });\n"
    )
    from cgir.js_verify import capture_js

    rec = capture_js(tmp_path, {"m.js:total": ("m.js", "total")})
    assert rec.get("m.js:total") == [[{"items": [1, 2]}]]


def test_jest_setup_snapshots_args_before_call(tmp_path):
    """Same pre-call-snapshot contract for the jest setup-file variant, driven
    under plain node (the setup file is self-contained CommonJS)."""
    import os

    import cgir.js_verify as jv

    target = tmp_path / "t.js"
    target.write_text(
        "function f(o) { o.items.push(99); return o.items.length; }\nmodule.exports = { f };\n"
    )
    setup = tmp_path / "setup.cjs"
    setup.write_text(jv._JS_JEST_SETUP)
    out = tmp_path / "rec.json"
    driver = tmp_path / "driver.cjs"
    driver.write_text(
        f"require({str(setup)!r});\n"
        f"const m = require({str(target)!r});\n"
        "m.f({ items: [1, 2] });\n"
    )
    env = {
        **os.environ,
        "CGIR_TARGETS": __import__("json").dumps([[str(target), "f"]]),
        "CGIR_CAP_OUT": str(out),
    }
    _sp.run(["node", str(driver)], env=env, check=True, capture_output=True)
    recs = {}
    for p in tmp_path.glob("rec.json.*"):
        recs.update(__import__("json").loads(p.read_text()))
    assert recs[f"{target}\x00f"] == [[[{"items": [1, 2]}], 3]]


def test_arg_mutation_does_not_poison_replay(tmp_path):
    """End to end: old reads a[0] THEN clears its argument. A broken candidate
    that reads after clearing is wrong on the real input ([10,20] -> undefined
    instead of 10) but agrees with old on the post-mutation input ([]). If
    capture snapshots args after the call, the replay runs on [] and the broken
    candidate falsely passes. Must diverge."""
    (tmp_path / "m.js").write_text(
        "function first(a) { const v = a[0]; a.length = 0; return v; }\n"
        "module.exports = { first };\n"
    )
    (tmp_path / "m.test.js").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert');\n"
        "const { first } = require('./m.js');\n"
        "test('first', () => { assert.strictEqual(first([10, 20]), 10); });\n"
    )
    _init_git(tmp_path)
    (tmp_path / "m.js").write_text(
        "function first(a) { a.length = 0; return a[0]; }\nmodule.exports = { first };\n"
    )
    verdicts = verify_diff_js(tmp_path, "HEAD")
    by = {v.qualname.split(":")[-1]: v for v in verdicts}
    assert by["first"].status == DIVERGED, by["first"]


# --- spaced paths in git diff output -----------------------------------------


def test_changed_js_functions_handles_spaced_paths(tmp_path):
    (tmp_path / "a b.js").write_text("function f(x) { return x; }\n")
    _init_git(tmp_path)
    (tmp_path / "a b.js").write_text("function f(x) { return x + 1; }\n")
    changed, _ = changed_js_functions(tmp_path, "HEAD")
    assert [c.func_name for c in changed] == ["f"]
    assert changed[0].path == "a b.js"


# --- jest detection + setupFilesAfterEnv merge -------------------------------


def _fake_capture(monkeypatch, tmp_path):
    import cgir.js_verify as jv

    seen = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _R()

    monkeypatch.setattr(jv.subprocess, "run", fake_run)
    (tmp_path / "x.js").write_text("function f(){return 1} module.exports={f};")
    return jv, seen, {"x.js:f": ("x.js", "f")}


def test_repo_jest_setup_files_package_json(tmp_path):
    import json as _json

    from cgir.js_verify import _repo_jest_setup_files

    (tmp_path / "package.json").write_text(
        _json.dumps({"jest": {"setupFilesAfterEnv": ["./jest.setup.js"]}})
    )
    assert _repo_jest_setup_files(tmp_path) == ["./jest.setup.js"]


def test_repo_jest_setup_files_config_js(tmp_path):
    from cgir.js_verify import _repo_jest_setup_files

    (tmp_path / "jest.config.js").write_text(
        "module.exports = {\n"
        "  verbose: true,\n"
        "  setupFilesAfterEnv: ['<rootDir>/jest.setup.js', \"./more.js\"],\n"
        "};\n"
    )
    assert _repo_jest_setup_files(tmp_path) == ["<rootDir>/jest.setup.js", "./more.js"]


def test_repo_jest_setup_files_absent(tmp_path):
    from cgir.js_verify import _repo_jest_setup_files

    assert _repo_jest_setup_files(tmp_path) == []


def test_jest_setup_merge_preserves_repo_setup_files(monkeypatch, tmp_path):
    """Our --setupFilesAfterEnv OVERRIDES the repo's config value; theirs must
    be re-passed first, ours appended — else jest.setup.js / custom matchers
    vanish, tests fail, and capture records nothing."""
    jv, seen, tgt = _fake_capture(monkeypatch, tmp_path)
    (tmp_path / "jest.config.js").write_text(
        "module.exports = { setupFilesAfterEnv: ['<rootDir>/jest.setup.js'] };\n"
    )
    jv.capture_js(tmp_path, tgt, test_cmd=["npx", "jest"])
    flags = [c for c in seen["cmd"] if c.startswith("--setupFilesAfterEnv=")]
    assert flags[0] == "--setupFilesAfterEnv=<rootDir>/jest.setup.js"
    assert len(flags) == 2 and flags[1].endswith("cgir_jest_setup.cjs")


def test_jest_detected_via_npm_test_script(monkeypatch, tmp_path):
    """`npm test` with jest underneath never matched `"jest" in part`; the
    package.json scripts.test value must be consulted. Flags go after `--` so
    npm forwards them to jest."""
    import json as _json

    jv, seen, tgt = _fake_capture(monkeypatch, tmp_path)
    (tmp_path / "package.json").write_text(_json.dumps({"scripts": {"test": "jest --ci"}}))
    jv.capture_js(tmp_path, tgt, test_cmd=["npm", "test"])
    cmd = seen["cmd"]
    setup_idx = [i for i, c in enumerate(cmd) if c.startswith("--setupFilesAfterEnv=")]
    assert setup_idx, cmd
    assert "--" in cmd and cmd.index("--") < setup_idx[0]


def test_npm_test_without_jest_script_gets_no_setup(monkeypatch, tmp_path):
    import json as _json

    jv, seen, tgt = _fake_capture(monkeypatch, tmp_path)
    (tmp_path / "package.json").write_text(_json.dumps({"scripts": {"test": "mocha"}}))
    jv.capture_js(tmp_path, tgt, test_cmd=["npm", "test"])
    assert not any("setupFilesAfterEnv" in c for c in seen["cmd"])


def test_jest_cmd_injects_setup_file(monkeypatch, tmp_path):
    """jest sandboxes its module registry, so capture must inject a setup file
    (--setupFilesAfterEnv) that wraps target exports inside jest — the require-
    hook alone is blind to jest. node --test must NOT get the flag."""
    import cgir.js_verify as jv

    seen = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _R()

    monkeypatch.setattr(jv.subprocess, "run", fake_run)
    (tmp_path / "x.js").write_text("function f(){return 1} module.exports={f};")
    tgt = {"x.js:f": ("x.js", "f")}

    jv.capture_js(tmp_path, tgt, test_cmd=["npx", "jest"])
    assert any("--setupFilesAfterEnv=" in c for c in seen["cmd"])

    jv.capture_js(tmp_path, tgt, test_cmd=["node", "--test"])
    assert not any("setupFilesAfterEnv" in c for c in seen["cmd"])
