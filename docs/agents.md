# CGIR for agents — setup and protocol

Agents are CGIR's first-class users: instead of grepping and reading whole
files, an agent queries the semantic index and contract-checks its own edits
before proposing them.

## 1. Serve the index over MCP

Scan once (or run `cgir watch` to keep it live), then register the server:

```jsonc
// .mcp.json at the repo root (Claude Code picks this up automatically)
{
  "mcpServers": {
    "cgir": {
      "command": "cgir",
      "args": ["mcp", "--index", ".cgir"]
    }
  }
}
```

Requires `cgir` on PATH with the mcp extra (`uv tool install "codegraph-ir[mcp]"`
or `uv pip install "codegraph-ir[mcp]"`).

## 2. Teach the agent the protocol (CLAUDE.md / AGENTS.md snippet)

Paste into the repo's agent instructions:

```markdown
## Semantic index (CGIR)

This repo has a semantic contract index served over MCP (server: `cgir`).
Prefer it over grepping:

- **Finding code**: `search(query)` — ranked free terms + contract
  predicates: `kind:pure`, `effects:net`, `effects:none`, `lexical:false`,
  `callers:>3`, `pins:pure`, `entrypoint:HTTP`, `lang:go`, `covered:false`.
  E.g. "effects:net lexical:false callers:>2" = verified network code with
  real fan-in — queries grep and vector search cannot answer.
- **Loading context to edit a component**: `pack(component_id)` — the
  minimal contract bundle (signature, effects, pins, callee interfaces,
  linked tests). Do NOT read whole files for context first.
- **Before changing a component**: `impact(component_id)` — blast radius:
  affected callers, entrypoints at risk, exactly which tests to run.
- **After drafting an edit**: `impact_of_change(repo, component_id,
  candidate)` — the radius narrowed by your edit's real contract delta;
  `verify(repo, component_id, candidate)` — contract-check before proposing.

Respect `Pinned:` lines in packs — they are hard invariants (`pure`,
`no-net`, `stable-signature`, `frozen`), enforced by the pre-commit hook
and CI. A rewrite that violates a pin will be rejected; don't attempt it.
```

## 3. What the agent gets out of it

- **Fewer tokens, better context**: a pack is ~60–800 tokens vs ~400–20,000
  for raw files, and benchmarks show it *beats* whole-file context as
  rewrite input (`docs/experiment-log.md`).
- **Fewer rejected edits**: `verify`/`impact_of_change` catch contract
  drift before the human (or the hook, or CI) does.
- **No hallucinated wiring**: packs name DI receivers (`self.svc.method` /
  `this.svc.method`), the exact field names an edit must use.

## 4. The enforcement backstop

The same contracts the agent consults are enforced deterministically:
`cgir hook install` (pre-commit), the [GitHub Action](./github-action.md)
(per-PR), and `cgir: ` pins in source. The agent can't drift from what the
index says without something noticing — that's the point.
