# Case study: catching an agent PR that silently altered two contracts

A real run of the `cgir diff` gate on a real repository — an Angular
front-end (~3.7k LOC TypeScript, 27 components). It shows the gate catching
two regressions of the kind a human skimming a large agent-written PR would
plausibly miss, deterministically and with zero LLM inference.

## The scenario

Two edits, both the sort of "helpful refactor" an agent produces:

**1. A pure UI handler silently gains a network call.** An agent "adds read
analytics" to a navigation handler:

```diff
   read(ch: ChapterListItem) {
+    fetch(`/api/analytics/read/${ch.chapter_no}`, { method: 'POST' });
     this.router.navigate(['/reader', this.novelId, ch.chapter_no]);
   }
```

`NovelDetailComponent.read` was a `pure_function`. It now makes an
unhandled, fire-and-forget network request on every click.

**2. A service silently stops persisting.** An agent "optimizes" create with
an optimistic return and forgets to actually POST:

```diff
   create(payload: NovelCreate) {
-    return this.http.post<NovelOut>(`${this.base}/novels`, payload);
+    // perf: optimistic create — return immediately, backend sync happens on next fetch
+    return of({ ...payload, id: Date.now() } as unknown as NovelOut);
   }
```

`NovelsService.create` was an `effect_adapter [net]`. It now returns a
fake object and **never reaches the server** — a silent data-loss bug. The
call site still compiles, still returns a `NovelOut`, still looks fine in a
diff review.

## Running the gate

Exactly what the GitHub Action runs — scan base, scan head, diff:

```bash
cgir scan <base-checkout> --out base-index
cgir scan <head-checkout> --out head-index
cgir diff base-index head-index \
  --fail-on effect-gain:net --fail-on effect-loss:net \
  --fail-on kind-change --fail-on purity-drop
```

## What it caught (exit code 1)

```
drift violations (4):
  ! NovelsService.create: lost effect(s) net
  ! NovelDetailComponent.read: gained effect(s) net
  ! NovelDetailComponent.read: kind changed pure_function -> effect_adapter
  ! NovelDetailComponent.read: purity dropped 1.0 -> 0.0
```

Both regressions fail the build. The first — the data-loss bug — is the one
that matters most and is the hardest to see by eye: nothing about
`return of({...})` looks wrong locally. The gate sees it because the
component's *contract* changed: it used to do `net`, now it doesn't.

## What this exercise surfaced about CGIR itself

`effect-loss` did not exist when this was first run. The `read` effect-gain
was caught immediately; the `create` data-loss was *reported* in the diff
(`effects: net -> nondeterm`) but no `--fail-on` rule failed on it — the
rule set only had `effect-gain`. Losing a `net`/`db` effect is as dangerous
as gaining one, so the symmetric `effect-loss[:tag]` rule was added
(red-green). This is the intended loop: running the gate on real code
exposes the next gate rule.

## Reproducing

The scenario is scripted against any checkout of the target repo. The two
edits above are the entire "PR"; the four commands above are the entire
gate. No API key, no network, no LLM — the verdict is a pure function of the
two scans.
