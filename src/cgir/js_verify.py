"""verify-diff for JavaScript / TypeScript — the JS analog of :mod:`cgir.verify_diff`.

Same three-bucket contract (preserving / diverged / unverified), same oracle idea
(run the repo's tests to record each changed function's real inputs, then run the
old and new versions on them), realised over Node instead of CPython:

* **the differential** runs in a Node harness — it reconstructs each function from
  its source (transpiling TypeScript via the repo's own ``typescript`` when
  needed), calls both versions on the recorded inputs, and deep-compares with the
  same float tolerance / NaN handling as the Python ``_eq``;
* **capture** runs the repo's test command under a CommonJS ``require`` hook that
  wraps the target module's export and records ``(args, result)`` as JSON;
* **changed functions** are found with the same tree-sitter grammar the TypeScript
  language adapter already uses.

Node is the only extra dependency, discovered at call time — absent it, every
verdict is an honest ``unverified``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgir.verify_diff import DIVERGED, UNVERIFIED, Verdict

_TS_EXTS = {".ts", ".tsx"}
_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}


def node_available() -> bool:
    return shutil.which("node") is not None


# The Node harness: reconstruct old + new from source, run on recorded inputs,
# deep-compare, print one JSON verdict. Kept as a string (written to a temp
# .cjs at call time) exactly like the Python capture harness — no packaging of
# a .cjs asset, and the versioned logic lives beside its caller.
_JS_REPLAY_HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");
const P = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
// P: {functionName, oldSrc, newSrc, argTuples, isTS, repo} — oldSrc/newSrc are
// the WHOLE module file (each version), not just the function, so the target
// resolves its module-level dependencies (regexes, tables, imports) exactly as
// it does at runtime. Evaluating the function alone would leave those undefined
// and make both versions throw identically — a false "preserving".

function esbuildTransform(src, loader) {
  try {
    const esbuild = require(require.resolve("esbuild", { paths: [P.repo] }));
    return esbuild.transformSync(src, { loader: loader, format: "cjs", target: "es2020" }).code;
  } catch (e) {
    if (e && e.code !== "MODULE_NOT_FOUND" && !/Cannot find module/.test(String(e.message))) throw e;
    return null;
  }
}

function transpile(src) {
  // TypeScript must be transpiled (esbuild, or typescript@5's transpileModule —
  // TS 7's native port drops it, so we feature-check).
  if (P.isTS) {
    const out = esbuildTransform(src, "ts");
    if (out !== null) return out;
    try {
      const ts = require(require.resolve("typescript", { paths: [P.repo] }));
      if (ts && typeof ts.transpileModule === "function") {
        return ts.transpileModule(src, { compilerOptions: { target: "ES2020", module: "CommonJS" } }).outputText;
      }
    } catch (e) { if (e && e.code !== "MODULE_NOT_FOUND" && !/Cannot find module/.test(String(e.message))) throw e; }
    const err = new Error("no usable TS transpiler in the repo (install esbuild or typescript@5)");
    err.cgir = "no-ts";
    throw err;
  }
  // Plain JS: CommonJS evaluates directly. ESM (import/export) must be normalised
  // to CJS first — esbuild if present, else an honest error.
  if (/^\s*(import|export)\s/m.test(src)) {
    const out = esbuildTransform(src, "js");
    if (out !== null) return out;
    const err = new Error("ESM module needs esbuild to replay (install esbuild in the repo)");
    err.cgir = "no-esbuild";
    throw err;
  }
  return src;
}

function reconstruct(moduleSrc, name) {
  const s = transpile(moduleSrc);
  const mod = { exports: {} };
  // A real require rooted in the repo so the module's own imports resolve.
  const req = createRequire(path.join(P.repo, "__cgir_replay__.js"));
  const factory = new Function(
    "module", "exports", "require", "__filename", "__dirname",
    '"use strict";\n' + s + "\n; return (typeof " + name + ' !== "undefined") ? ' + name +
      " : (module.exports && (module.exports." + name + " || (module.exports.default && module.exports.default." + name + ")));"
  );
  const fn = factory(mod, mod.exports, req, path.join(P.repo, "__cgir_replay__.js"), P.repo);
  if (typeof fn !== "function") { const e = new Error("'" + name + "' is not a function after evaluating the module"); e.cgir = "not-fn"; throw e; }
  return fn;
}

function eq(a, b) {
  if (typeof a === "number" && typeof b === "number") {
    if (Number.isNaN(a) && Number.isNaN(b)) return true;
    if (!isFinite(a) || !isFinite(b)) return a === b;
    return Math.abs(a - b) <= Math.max(1e-9 * Math.max(Math.abs(a), Math.abs(b)), 1e-12);
  }
  if (typeof a !== typeof b) return false;
  if (a === null || b === null || a === undefined || b === undefined) return a === b;
  const aArr = Array.isArray(a), bArr = Array.isArray(b);
  if (aArr !== bArr) return false;
  if (aArr) return a.length === b.length && a.every((x, i) => eq(x, b[i]));
  if (typeof a === "object") {
    const ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && eq(a[k], b[k]));
  }
  return a === b;
}

function show(v) { try { return JSON.stringify(v); } catch (e) { return String(v); } }
function done(status, detail, checked) { process.stdout.write(JSON.stringify({ status, detail, checked: checked || 0 })); process.exit(0); }

let oldFn, newFn;
try { oldFn = reconstruct(P.oldSrc, P.functionName); }
catch (e) { done("unverified", "old version: " + e.message); }
try { newFn = reconstruct(P.newSrc, P.functionName); }
catch (e) { done("unverified", "new version: " + e.message); }
if (!P.argTuples.length) done("unverified", "no recorded inputs (not exercised by the test suite)");

for (const args of P.argTuples) {
  let oe = null, ne = null, oo, no;
  try { oo = oldFn.apply(null, args); } catch (e) { oe = e; }
  try { no = newFn.apply(null, args); } catch (e) { ne = e; }
  const ins = show(args);
  if (oe || ne) {
    if (oe && ne && (oe.name === ne.name)) continue; // same error type = preserved
    if (oe && ne) done("diverged", "on " + ins + ": old threw " + oe.name + ", new threw " + ne.name);
    if (oe) done("diverged", "on " + ins + ": old threw " + oe.name + ", new returned " + show(no));
    done("diverged", "on " + ins + ": old returned " + show(oo) + ", new threw " + ne.name);
  }
  if (!eq(oo, no)) done("diverged", "on " + ins + ": old returned " + show(oo) + ", new returned " + show(no));
}
done("preserving", "agreed on " + P.argTuples.length + " input(s)", P.argTuples.length);
"""


def differential_replay_js(
    repo: Path,
    qualname: str,
    func_name: str,
    old_src: str,
    new_src: str,
    arg_tuples: list[list[Any]],
    *,
    is_ts: bool = False,
) -> Verdict:
    """Run ``old_src`` and ``new_src`` (JS/TS function definitions) on every
    input in ``arg_tuples`` in Node and require identical results (or
    identically-named exceptions). Inputs are JSON values (as capture records
    them). Returns the first divergence as a counterexample, else preserving."""
    if not node_available():
        return Verdict(qualname, UNVERIFIED, "node not on PATH (needed for JS/TS replay)")
    work = Path(tempfile.mkdtemp(prefix="cgir-jsdiff-"))
    harness = work / "replay.cjs"
    harness.write_text(_JS_REPLAY_HARNESS)
    payload = work / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "functionName": func_name,
                "oldSrc": old_src,
                "newSrc": new_src,
                "argTuples": arg_tuples,
                "isTS": is_ts,
                "repo": str(repo.resolve()),
            }
        )
    )
    try:
        proc = subprocess.run(
            ["node", str(harness), str(payload)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return Verdict(
            qualname, DIVERGED, "replay timed out (a version is non-terminating on some input)"
        )
    out = proc.stdout.strip()
    if not out:
        return Verdict(
            qualname, UNVERIFIED, f"node replay produced no verdict: {proc.stderr.strip()[:200]}"
        )
    try:
        v = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        return Verdict(qualname, UNVERIFIED, f"unparseable node verdict: {out[:200]}")
    return Verdict(qualname, v["status"], v["detail"], checked=v.get("checked", 0))


# --- the git + tree-sitter layer: what changed ------------------------------


@dataclass
class ChangedJsFunction:
    qualname: str  # "path:name"
    path: str  # repo-relative
    func_name: str
    old_src: str
    new_src: str
    is_ts: bool


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _js_func_defs(source: str, ext: str) -> dict[str, str]:
    """name -> source_text for top-level function declarations and top-level
    ``const/let name = (…) => …`` / ``= function …`` bindings, including those
    behind ``export``. Uses the same tree-sitter grammar as the TS adapter."""
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Parser

    lang = tsts.language_tsx() if ext == ".tsx" else tsts.language_typescript()
    try:
        tree = Parser(Language(lang)).parse(source.encode())
    except Exception:
        return {}
    src = source.encode()
    out: dict[str, str] = {}

    def text(node: Any) -> str:
        return src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def unwrap(node: Any) -> Any:
        return node.children[-1] if node.type == "export_statement" and node.children else node

    for top in tree.root_node.children:
        node = unwrap(top)
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
            if name:
                out[text(name)] = text(top)  # keep the `export` prefix; harness strips it
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for decl in node.children:
                if decl.type != "variable_declarator":
                    continue
                name = decl.child_by_field_name("name")
                value = decl.child_by_field_name("value")
                if (
                    name
                    and value
                    and value.type in ("arrow_function", "function_expression", "function")
                ):
                    out[text(name)] = text(top)
    return out


def changed_js_functions(repo: Path, base_ref: str) -> tuple[list[ChangedJsFunction], list[str]]:
    """JS/TS functions whose source changed between ``base_ref`` and the working
    tree. Returns (changed, notes)."""
    changed: list[ChangedJsFunction] = []
    notes: list[str] = []
    globs = [f"*{e}" for e in sorted(_JS_EXTS)]
    try:
        names = _git(repo, "diff", "--name-only", base_ref, "--", *globs).split()
    except subprocess.CalledProcessError as exc:
        return [], [f"git diff failed (is {base_ref!r} a valid ref?): {exc.stderr.strip()}"]
    for rel in names:
        new_file = repo / rel
        ext = Path(rel).suffix
        if not new_file.exists() or ext not in _JS_EXTS:
            continue
        try:
            old_source = _git(repo, "show", f"{base_ref}:{rel}")
        except subprocess.CalledProcessError:
            continue  # newly added
        new_source = new_file.read_text(errors="replace")
        old_defs = _js_func_defs(old_source, ext)
        new_defs = _js_func_defs(new_source, ext)
        for name in new_defs:
            # detect change at function granularity, but carry the WHOLE file
            # (old + new) so replay resolves module-level deps like the real module.
            if name in old_defs and old_defs[name] != new_defs[name]:
                changed.append(
                    ChangedJsFunction(
                        f"{rel}:{name}", rel, name, old_source, new_source, ext in _TS_EXTS
                    )
                )
    return changed, notes


# --- capture: record real inputs by running the repo's tests ----------------

# A CommonJS require-hook preload (NODE_OPTIONS=--require). When a target module
# loads, its exported target function is wrapped to record (args, result) as
# JSON. Works with the real module system (node:test, mocha, plain node);
# jest/vitest run tests in their own module sandbox and need a setup-file wrap
# instead (documented). Async returns (Promises) are skipped — can't snapshot.
_JS_CAPTURE_HOOK = r"""
"use strict";
const Module = require("module");
const fs = require("fs");
const TARGETS = JSON.parse(process.env.CGIR_TARGETS);   // [[absfile, name], ...]
const OUT = process.env.CGIR_CAP_OUT;
const records = {};
function jsonSafe(v){ try { return JSON.parse(JSON.stringify(v === undefined ? null : v)); } catch(e){ return Symbol.for("cgir-unsafe"); } }
const UNSAFE = Symbol.for("cgir-unsafe");
const orig = Module._load;
Module._load = function(request, parent, isMain){
  const m = orig.apply(this, arguments);
  try {
    const resolved = Module._resolveFilename(request, parent, isMain);
    for (const [file, name] of TARGETS){
      if (resolved === file && m && typeof m[name] === "function" && !m[name].__cgirWrapped){
        const fn = m[name];
        const wrapped = function(){
          const args = Array.prototype.slice.call(arguments);
          const ret = fn.apply(this, args);
          if (!(ret && typeof ret.then === "function")) {  // skip async
            const a = jsonSafe(args), r = jsonSafe(ret);
            if (a !== UNSAFE && r !== UNSAFE){ const k = file + "\x00" + name; (records[k] = records[k] || []).push([a, r]); }
          }
          return ret;
        };
        wrapped.__cgirWrapped = true;
        try { m[name] = wrapped; } catch(e){}
      }
    }
  } catch(e){}
  return m;
};
// `node --test` runs each test file in a CHILD process; both parent and child
// carry this hook via NODE_OPTIONS. Write a per-pid file (the parent's is empty,
// the child's has the records) and let the Python side merge them — a single
// shared file would let the empty parent clobber the child's data on exit.
process.on("exit", () => { try { fs.writeFileSync(OUT + "." + process.pid, JSON.stringify(records)); } catch(e){} });
"""

# jest (and vitest) run tests in a sandboxed module registry, so the require-hook
# above never sees the target module. Injected instead as a jest setup file
# (--setupFilesAfterEnv), which runs INSIDE each test file's registry: it
# requires the target by its absolute path (the same key the test file's
# `require('./x')` resolves to) and wraps the export there. Records flush on
# jest's afterAll and on exit; a random filename suffix keeps parallel workers
# from clobbering each other (merged Python-side like the child-process case).
_JS_JEST_SETUP = r"""
"use strict";
const fs = require("fs");
const TARGETS = JSON.parse(process.env.CGIR_TARGETS);
const OUT = process.env.CGIR_CAP_OUT;
const records = {};
function jsonSafe(v){ try { return JSON.parse(JSON.stringify(v === undefined ? null : v)); } catch(e){ return Symbol.for("cgir-unsafe"); } }
const UNSAFE = Symbol.for("cgir-unsafe");
function wrap(){
  for (const [file, name] of TARGETS){
    let m; try { m = require(file); } catch(e){ continue; }
    if (m && typeof m[name] === "function" && !m[name].__cgirWrapped){
      const fn = m[name];
      const wrapped = function(){
        const args = Array.prototype.slice.call(arguments);
        const ret = fn.apply(this, args);
        if (!(ret && typeof ret.then === "function")){
          const a = jsonSafe(args), r = jsonSafe(ret);
          if (a !== UNSAFE && r !== UNSAFE){ const k = file + "\x00" + name; (records[k] = records[k] || []).push([a, r]); }
        }
        return ret;
      };
      wrapped.__cgirWrapped = true;
      try { m[name] = wrapped; } catch(e){}
    }
  }
}
wrap();
function flush(){ try { fs.writeFileSync(OUT + "." + process.pid + "." + Math.random().toString(36).slice(2), JSON.stringify(records)); } catch(e){} }
if (typeof afterAll === "function") { try { afterAll(flush); } catch(e){} }
process.on("exit", flush);
"""


def capture_js(
    repo: Path,
    targets: dict[str, tuple[str, str]],
    *,
    test_cmd: list[str] | None = None,
    timeout: int = 900,
) -> dict[str, list[list[Any]]]:
    """Run the repo's tests with each target function wrapped; return
    ``{qualname: [args, ...]}`` (recorded input tuples). ``targets`` maps
    qualname -> (repo-relative file, export name). ``test_cmd`` defaults to
    ``node --test`` (override for mocha etc.)."""
    if not node_available():
        return {}
    repo = repo.resolve()
    abs_targets = [[str((repo / f).resolve()), name] for _, (f, name) in targets.items()]
    key_to_qual = {f"{(repo / f).resolve()!s}\x00{name}": q for q, (f, name) in targets.items()}
    work = Path(tempfile.mkdtemp(prefix="cgir-jscap-"))
    hook = work / "hook.cjs"
    hook.write_text(_JS_CAPTURE_HOOK)
    out_file = work / "records.json"
    env = {
        **__import__("os").environ,
        "CGIR_TARGETS": json.dumps(abs_targets),
        "CGIR_CAP_OUT": str(out_file),
        "NODE_OPTIONS": f"--require {hook}",
    }
    cmd = list(test_cmd or ["node", "--test"])
    # jest/vitest sandbox their module registry, so the require-hook is blind to
    # the target calls. Inject a setup file that wraps exports inside jest's own
    # registry (the require-hook still runs, harmlessly, for anything outside it).
    if any("jest" in part for part in cmd):
        setup = work / "cgir_jest_setup.cjs"
        setup.write_text(_JS_JEST_SETUP)
        cmd += [f"--setupFilesAfterEnv={setup}"]
    try:
        subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    # Merge every per-pid records file (parent + test-file children).
    merged: dict[str, list[Any]] = {}
    for rec in work.glob("records.json.*"):
        try:
            data = json.loads(rec.read_text() or "{}")
        except json.JSONDecodeError:
            continue
        for key, pairs in data.items():
            merged.setdefault(key, []).extend(pairs)
    result: dict[str, list[list[Any]]] = {}
    for key, pairs in merged.items():
        q = key_to_qual.get(key)
        if q is not None and pairs:
            result[q] = [args for args, _ret in pairs]
    return result


# --- orchestration ----------------------------------------------------------


def verify_diff_js(
    repo: Path,
    base_ref: str = "HEAD",
    *,
    fuzz_rounds: int = 0,
    test_cmd: list[str] | None = None,
) -> list[Verdict]:
    """End to end for JS/TS: find changed functions, record their real inputs by
    running the repo's tests, and differentially replay old vs new in Node."""
    from cgir.verify_diff import fuzz_around

    if not node_available():
        return []
    changed, _notes = changed_js_functions(repo, base_ref)
    if not changed:
        return []
    targets = {cf.qualname: (cf.path, cf.func_name) for cf in changed}
    recorded = capture_js(repo, targets, test_cmd=test_cmd)
    verdicts: list[Verdict] = []
    for cf in changed:
        inputs = [list(a) for a in recorded.get(cf.qualname, [])]
        n_fuzz = 0
        if fuzz_rounds and inputs:
            fuzzed = [list(a) for a in fuzz_around([tuple(a) for a in inputs], fuzz_rounds)]
            inputs += fuzzed
            n_fuzz = len(fuzzed)
        v = differential_replay_js(
            repo, cf.qualname, cf.func_name, cf.old_src, cf.new_src, inputs, is_ts=cf.is_ts
        )
        v.fuzzed = n_fuzz if v.status != UNVERIFIED else 0
        verdicts.append(v)
    return verdicts
