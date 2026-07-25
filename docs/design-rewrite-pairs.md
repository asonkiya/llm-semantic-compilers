# Rewrite-pair registry — de-nicheing the rewrite engine

The analysis layer already has plug-and-play adapters: `src/cgir/languages/`
exposes a `LanguageAdapter` ABC + a `cgir.languages` entry-point group, so
`pip install cgir-<lang>` adds a language and every downstream pass (symbols,
CFG, effects, purity, slicing, ComponentSpec, MCP tools) works on it. Five
languages ship today (Python, TypeScript, Go, Rust, C).

The **rewrite** layer never got the same treatment. This doc traces exactly
what's hardcoded, and specifies the registry that fixes it.

## The trace: what's hardcoded vs. shared

The verified-rewrite engine already has a language-neutral core —
`cgir.rewrite.run_search_loop` — that knows nothing about languages:

```
run_search_loop(items, *, build_prompt, evaluate, id_of, sampler, k,
                budget_usd, ledger_path, report_meta, log)
  # k cheap candidates per item -> evaluate (first "ok" wins)
  # -> one escalation carrying the failure feedback
  # -> resumable per-item ledger, hard budget cap
```

Both `run_c_rust` (`rewrite_c_rust.py`) and `run_python_rust`
(`rewrite_python_rust.py`) are just *wiring*: they build three closures and
hand them to that loop. Everything pair-specific lives in exactly three places:

| seam | c-rust | python-rust |
|---|---|---|
| **worklist** (eligibility → items) | `c_rust_worklist` (regex ABI over the index) | `python_rust_worklist` (ast-typed leaves) |
| **build_prompt** | `build_c_rust_prompt` (+ compiler-probed context) | `build_python_rust_prompt` |
| **evaluate** (compile + oracle) | `try_rustc` → `contract_check` → `differential` vs compiled C | `try_rustc` → `exported_symbols` → `replay_against_dylib` on recorded traces |
| **apply** (optional) | `link_back` (patch the C TU) | ctypes/PyO3 wrapper emission |

And the only thing routing between them is a hardcoded ladder in
`cli.py:rewrite_cmd`:

```python
if lang == "c-rust":     _rewrite_c_rust(...);     return
if lang == "python-rust": _rewrite_python_rust(...); return
...
raise BadParameter("--lang must be python, c-rust, or python-rust")
```

So the niche is **not** architectural. The core is neutral; the oracle
(`ffi/driver.py` differential, `ffi/gate.py` whole-program gate) compares
compiled artifacts over the C ABI and is language-agnostic; the IR
(`ffi/ir.py`) speaks scalars/buffers/structs, not "C". What's missing is the
plug-and-play *seam* the analysis layer already has.

## The registry

`src/cgir/rewrite_pairs/` mirrors `languages/` exactly:

```python
class RewritePair(Protocol):
    name: str            # "c-rust", "python-python"
    description: str      # one line, shown in --help / list
    # eligibility: index + source -> (items, excluded[(id, reason)])
    def worklist(self, index_dir, source, opts) -> tuple[list, list]
    def prepare(self, entries, opts) -> PairContext   # compile oracle, probe, workdir
    def build_prompt(self, entry, ctx) -> str
    def evaluate(self, entry, candidate, ctx) -> tuple[str, str, dict]  # (stage, err, meta)
    def apply(self, winners, ctx, opts) -> dict       # optional; default: not supported
```

- **`SearchLoopPair`** — a base class implementing a single `run(...)` from those
  methods via `run_search_loop`. A new pair author writes the four small methods
  and inherits the search loop, ledger, budget, escalation, and reporting — the
  same "one adapter, not a re-implemented pipeline" deal the analysis layer gives.
- **Registry + discovery** (`registry.py`): builtins + a `cgir.rewrite_pairs`
  entry-point group, with the identical safety rules as `languages/registry.py`
  (broken plugin → warning not crash, builtins win name conflicts, API-version
  mismatch loads with defaults). `pip install cgir-<pair>` is the integration.
- **CLI**: `rewrite_cmd` resolves `--lang` against the registry instead of the
  hardcoded ladder; unknown pairs list what's installed.

The two existing engines are wrapped as builtins (their proven `run_c_rust` /
`run_python_rust` become the pair's `run`), so nothing about the verified C→Rust
path changes — it just becomes discoverable and uniform. New pairs use the
fine-grained `SearchLoopPair` seam.

## Why this de-niches (the point)

The highest-leverage first new pair is **not** another exotic cross-language
migration — it's **same-language** (`python-python`): "an LLM/refactor changed
this function; prove it's behavior-preserving." That rides the *same* neutral
core and the *same* recorded-trace / test-suite oracle the python-rust pair
already uses, and it turns the engine from "niche C→Rust migrator" into "the
behavior-preservation gate for any AI-authored diff" — the broad developer
market. C→Rust stays as one premium pair among many.
