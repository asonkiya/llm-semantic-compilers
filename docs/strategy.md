# Strategy: what CGIR should become (researched 2026-07-05)

## Landscape findings

**1. "Code graph + MCP for agents" is commoditizing.** codegraph, tokensave,
codebase-memory-mcp, CodeGraphContext, Code Pathfinder all ship tree-sitter
graphs (symbols, calls, imports, routes) over MCP, some with vector search,
claiming 35% cost / 70% fewer tool calls / "120x fewer tokens". CGIR's
*structural* layer is table stakes. None of them do effects, purity,
contracts, or verification — CGIR's *semantic* layer has no shipped peer.

**2. Context packing is a solved commodity.** Repomix (26k stars, ~255k
downloads/mo) owns dumb packing; aider's repo-map owns ranked packing.
Competing head-on there is a losing game. Our own experiment showed
structural context isn't the bottleneck anyway — *contract completeness* is.

**3. Verification of LLM-written changes is papers, not products.**
SemGuard, contract-inference, semantic change-impact — all academic. No
shipped tool answers "did this AI change alter the component's contract?"
deterministically. `cgir diff --fail-on effect-gain` is already ahead of the
shipped market. **This niche is empty.**

**4. AI code review is a real market with a determinism gap.** CodeRabbit
(2M+ repos) reviews diffs; Greptile ($20-30/user/mo) differentiates on
"architectural drift" via a semantic graph — but its findings are LLM
judgments with a reported 30–50% manual-triage false-positive burden.
CGIR's drift detection is *static, deterministic, zero-inference-cost*:
a different and complementary category, the way ruff relates to a human
reviewer.

**5. Our rewrite experiment (12 components, camera-tracking, Sonnet 4.6):**
pack-only 4/12 vs stubbed-file 8/12. Failures were *not* algorithmic — the
model reconstructed ray casting and OAuth flows fine but guessed wrong about
data shapes (`Point` tuple vs attrs), config keys, module constants. Three
components failed under *both* conditions where tests pin exact semantics.
When the contract sufficed, compression was 20–300x (get_summary: 64 tokens
vs 20,198). Diagnosis: type closure > behavior pinning (tests/docstrings) >
module-constant closure.

## The pivot

Stop positioning CGIR as "context compression so LLMs can rewrite pure
functions" (premise eroded by big windows + agentic grep; easy cases don't
need it; crowded adjacents). Reposition as:

> **The deterministic contract layer for AI-modified codebases.**
> Agents write more and more of the code; CGIR is the cheap, static,
> hallucination-free gate that says what each component *is* (effects,
> purity, types, entrypoints, call surface) and whether a change *altered
> it*. Ruff for architecture.

Three product surfaces, one pipeline:

1. **Gates (CI)** — `cgir diff --fail-on effect-gain:net,purity-drop` as a
   GitHub Action. Deterministic, per-PR, no per-seat LLM cost. The wedge:
   teams drowning in agent-written PRs need guardrails that never
   hallucinate. Unique today.
2. **Self-verification (agents)** — `cgir verify <id> --candidate` : splice,
   re-scan, contract-diff, run linked tests. Exposed over MCP so agents
   check their own edits before proposing them. The experiment harness is
   80% of this code already.
3. **Comprehension (humans)** — viz, stats, flow, entrypoints. Keep as the
   demo/adoption surface; it's what makes people *believe* the graph.

`pack` stays but is repositioned as the *contract bundle* feeding 2, and is
only credible after the evidence-ranked fixes: type closure, docstring +
exception extraction, test linkage, module-constant closure.

**Decompose** (PDG-sliced functional-core/imperative-shell suggestions)
remains the long-term flagship — no shipped peer — but comes after the
loop closes.

## Ordered roadmap

1. **Contract enrichment** (evidence-ranked from the experiment):
   type closure in pack, docstrings + raises into specs, test linkage
   (Test NodeKind), module-constant closure. Re-run the experiment;
   target pack ≥ 8/12 at <800 avg tokens. The result is the marketing
   artifact ("rewrite-readiness benchmark").
2. **`cgir verify`** — productize the experiment harness.
3. **GitHub Action** (`cgir-action`): scan base/head, diff, fail-on rules,
   PR comment with the drift table.
4. **TypeScript ingester** — most agent-heavy repos are TS; the user's own
   projects are TS; grammar-agnostic seams exist.
5. **`cgir decompose`** — PDG slicing suggestions + verify loop = repos
   *become* decomposable.
6. De-emphasize: LLM regeneration (agents already generate; CGIR checks),
   Neo4j/Joern/CodeQL bridges (enterprise later), generic packing (repomix
   owns it).

## Sources

- https://www.bighatgroup.com/blog/codegraph-2026-05-26/
- https://github.com/aovestdipaperino/tokensave
- https://deusdata.github.io/codebase-memory-mcp/
- https://github.com/CodeGraphContext/CodeGraphContext
- https://dev.to/corestory/mcp-servers-for-codebase-context-how-ai-coding-agents-access-code-intelligence-3757
- https://repomix.com/ and https://aider.chat/docs/repomap.html
- https://rywalker.com/research/code-intelligence-tools
- https://arxiv.org/pdf/2509.24507 (SemGuard) and https://arxiv.org/pdf/2510.12702 (LLM contract inference)
- https://www.greptile.com/content-library/best-ai-code-review-tools
- https://baeseokjae.github.io/posts/ai-code-review-tools-2026/
