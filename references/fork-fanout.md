# Explore once, fork many

Many tasks have the same shape: gather context, then do several separate
things with it. Examples are specialists that each take one angle, deep dives
into separate parts, or alternatives built from the same facts. If each worker
starts fresh, each one re-reads the context at full input price and spends
turns finding it again. Instead, have one explorer read the material into its
session, then start every worker as a native fork of that session. Each child
begins with the explorer's full history (the files it read, verbatim), and the
provider serves that shared prefix from its prompt cache. You pay for the
exploration once. The other N workers pay the cached-input rate for the shared
prefix, plus their own work.

Use this pattern by default whenever three or more workers would otherwise read
the same material, whatever the task. `examples/fork-fanout.py` is a general
runner: pass it the context to gather and a list of tasks.
`examples/fork-review.py` is one instance, code review with a verify stage.

## Where it fits

Look for a phase that gathers context and is followed by work that splits.
Each child's prompt names one slice of that work:

| Shape | Explorer gathers | Each fork |
|---|---|---|
| Specialist lenses | a change, design doc, contract or config | reviews or audits it for one concern (security, performance, API compatibility, accessibility, compliance) |
| Question fan-out | a codebase area, corpus, dataset or log set | answers one question, with citations |
| Hypothesis fan-out | a bug report, reproduction, logs and the suspect code | pursues one hypothesis to confirm or rule it out |
| Per-unit planning | a library's new API plus the call sites to migrate | plans one module's migration; fresh writers then apply each plan in its own worktree |
| Alternatives | requirements, constraints and the existing system | drafts one design or approach for a judge panel to compare |
| Deep-dive tree | the overview of a large system | explores one subsystem, and forks its own children from there |
| Parallel authoring | the source material and an outline | writes one section, test file or doc page |
| Repeated rounds | the target | runs one finder round of a loop-until-dry search, so later rounds skip re-exploration |

The shapes combine with the other patterns. For example, fork the finders of a
loop-until-dry search, fork the members of a judge panel, or fork the analysts
whose plans feed an issue swarm.

## What it saves

Take a 200k-token explorer context and 15 workers:

| | Shared-context input the 15 workers pay for | Exploration turns |
|---|---|---|
| 15 fresh agents | 15 × 200k at full price, plus each one's search turns | 15 |
| explorer + 15 forks | 200k once (+ cache write), then 15 × 200k at the cached rate | 1 |

Cached input costs about 0.1× on Anthropic and OpenAI (1.25× to write it
once), so the shared part costs roughly 10× less. Forks also finish sooner,
because they skip exploration. The saving applies to the shared prefix only.
Each child's own output, its extra reads, and its prompt cost the same as
before. Provider figures as of September 2026:

- **Anthropic (claude_code route `opus`):** read 0.1× base input
  (lower on some newer models), 5-minute write 1.25×, 1-hour write 2×. Every
  read refreshes the TTL. `claude -p` uses a 1h TTL on a subscription within
  plan usage and 5m on an API key; `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` raises an
  API-key route to 1h. Cache reads still count toward plan usage, at the cached
  rate.
- **OpenAI (codex routes `luna`, `sol`, `astra`; `pi-sol`):** cached
  input 0.1×, minimum 1,024 tokens, and a prefix stays warm about 30 minutes,
  refreshed on each reuse. ChatGPT-plan Codex credits charge no write premium.
- **Z.ai GLM (`glm`, `pi-glm`):** caching is implicit. The coding-plan cached
  rate is about 24% of uncached, so the saving is about 76%. Whether caching
  works through the Anthropic-compatible endpoint that `glm` uses has not been
  verified. Measure it (below) before relying on a large glm fan-out.

## The rules that make the cache hit

A fork only saves anything if the child's first request starts with exactly the
bytes the explorer already cached. `wf.fork()` handles most of this for you:
by default it keeps the parent's route, model, effort, `dir`, `posture` and
`add_dirs`.

1. **Same route, model and effort.** Do not pass `route=`, `model=` or
   `effort=` to a fork. A different model has a different cache, and on most
   providers a different effort or tool set rewrites the prefix. A child on
   another route is not a native fork at all (see rule 5).
2. **Same directory.** Claude Code finds a session only from the directory it
   was created in, and a child in any other directory fails with "No
   conversation found". opencode and pi put the working directory into the
   system prompt, so a different `dir` misses the whole cache. Never give a
   fork its own worktree. If children must write files, see "When not to fork".
3. **Same posture and context mode.** pi and prime-agent build their system
   prompt from the posture's tool list, and the `glm` and `deepseek` launchers
   put a posture guard into it, so a posture change misses the cache. Only the
   Anthropic launcher (`opus`) is posture-neutral. Never change
   posture on a fork.
4. **Start children promptly and keep them busy.** Every child's first request
   reads the prefix and refreshes its TTL. With a route capped at
   `max_concurrency` 3–4, 15 forks run in 4–5 waves. The prefix stays warm as
   long as a new child starts within one TTL of the last. For wide fan-outs,
   raise the cap for this run with a `--routes` file (a path, not inline JSON)
   containing `{"glm": {"max_concurrency": 8}}`.
5. **Keep the explorer on a fork-capable harness and pass
   `require_native=True`.** Forks run on `glm`, `deepseek`, `opus`
   (claude_code), `luna`, `sol`, `astra` (codex), and `pi-glm`,
   `pi-muse`, `pi-sol` (pi). They do not run on `gemini` or `muse`. The
   cheap routes fall back to non-fork routes, so give the explorer
   `fallback=[]`. Otherwise one rate-limit can move the explorer to `muse`, and
   every "fork" silently degrades into a fresh agent that receives only the
   explorer's final answer. `require_native=True` turns that degradation into a
   clear failure. The forks themselves never fall back to another route; a
   failed fork retries its own route once.
6. **Never resume the explorer.** A fork copies the session as it is now. If
   anything runs `resume=explorer.session_id`, later forks inherit those turns,
   and a writer and readers end up sharing one session file. To continue the
   explorer, fork it.

## Writing the explorer

- Make it gather, not work. It should open the material with its file tools:
  the spec, the code, the logs, the change and what that change depends on.
  Children inherit tool results verbatim, and those are what they will not have
  to re-read. Tell it not to start on any child's task, because its
  conclusions would bias every child. In review posture a `pi-*` explorer has
  no shell, so give it files (for a change,
  `git diff origin/main...HEAD > change.patch`) rather than a command.
- Never run the explorer with `isolation="worktree"`: its clean worktree is
  removed when it finishes, and forks must run in the explorer's directory.
- End it with a short reply. The explorer's final message and each child's
  prompt are the only parts a child pays full price for. A map of what it read
  (at most ~60 lines of `source - what it holds`) is useful and cheap. Do not
  end with a summary of the material.
- Keep it under about half the model's window. Children need room for their own
  work. A child that runs out of window auto-compacts, which replaces the shared
  prefix with a summary and loses the cache (and detail). Budget 10–20 tokens
  per line of code or text: a 20,000-line PR or codebase slice is about
  200k–400k tokens before surrounding context. That needs a long-context model (e.g. `model="opus[1m]"`
  on the explorer, which its forks inherit) or sharding.

## Sharding context too big for one explorer

Split the material into K groups that belong together, for example by
subsystem, document set or time window. Run one explorer per group, and fork
each group's workers from that group's explorer. A shard explorer never read
the other groups, so check any output that cites material outside its group
with a fresh agent in `dir=root`, not with a fork. The synthesizer can only
correlate what the workers reported. For effects that span groups (a caller in
one breaking a callee in another), add a fresh agent in `dir=root` over the
groups' maps and the interfaces between them. The snippet below shows code
review; for findings, keep the verify stage from `examples/fork-review.py`.

Shards on one route compete for its `max_concurrency` slots, first come first
served. A later shard's explorer prefix can go cold while it waits. Run the
shards one after another, or raise the route's cap to about K times one shard's
width. This snippet uses the prompt and schema constants from
`examples/fork-review.py`:

```python
async def main(wf, args):
    async def shard(group, g):
        ex = await wf.agent(EXPLORE.format(target=group), route="opus", dir=args["root"], fallback=[], label=f"explore-{g}")
        if not ex:
            return []
        reviews = await wf.parallel([
            (lambda d=d: wf.fork(ex, REVIEW.format(dim=d, key=d, focus=""), schema=FINDINGS, require_native=True,
                                 label=f"review-{g}:{d}", phase="Review"))
            for d in args["dimensions"]])
        return [dict(f, group=g, dimension=d) for d, r in zip(args["dimensions"], reviews) if r for f in r.data["findings"]]
    groups = await wf.pipeline(args["groups"], shard)
    findings = [f for g in groups if g for f in g]
    report = await wf.agent("Merge and rank these findings; re-open the code for any cross-group interaction:\n"
                            + json.dumps(findings), route="opus", dir=args["root"], label="synthesize")
    return {"findings": findings, "report": report.text}
```

A fork can itself be forked. For example, an explorer can fork one deep-dive
per subsystem, and each deep-dive can fork its own specialists. Each level
inherits everything above it, so each level must also fit in the window.

## When not to fork

- **Independent verification or judging.** A child inherits everything its
  parent concluded. Never verify a result by forking the agent that produced
  it. Forking the *explorer* is fine for verification, because the explorer
  only read and never judged, and it is cheap. For the most independence, use a
  fresh agent on a different model (`examples/fork-review.py` takes
  `verify_route` for this). The same holds for alternatives: forks of one
  explorer share its facts, which is what you want, but a judge of those
  alternatives should not be one of them.
- **Children that write files.** Parallel writers need separate worktrees, and a
  separate worktree breaks rule 2. Fork read-only analysts, then hand their
  plans to fresh writers with `isolation="worktree"`.
- **Small context.** Below the provider's minimum cacheable prefix (512–1,024
  tokens), or when each child reads mostly different files, a fork buys
  little. Plain `wf.agent()` calls are simpler.
- **Routes that cannot fork.** Move the explorer to a fork-capable route rather
  than relying on the context fallback.

## Checking that the cache is hitting

Before running a 15-way fan-out on a route for the first time, run the explorer
and two forks with `"format": "json"` on the route (in a `--routes` file). Then
read the usage in each child's stream (`stream.log` or `stream.jsonl`, depending
on the provider):

```bash
grep -ho '"cache_read_input_tokens":[0-9]*' "$RUN_DIR"/stream.* | tail -1   # claude_code (all providers)
grep -ho '"cached_input_tokens":[0-9]*' "$RUN_DIR"/stream.* | tail -1       # codex
grep -ho '"cacheRead":[0-9]*' "$RUN_DIR"/stream.* | tail -1                 # pi
```

`RUN_DIR` is the child's `result.run_dir`, which is also recorded in
`steps/<key>/result-N.json` under the workflow run directory. A child's
cached tokens should be close to the explorer's context size. If they are near
zero, one of the rules above is being broken. To go on to the full fan-out,
`--resume` the probe run with the full task list. The explorer is then
served from the journal, and the new forks branch from its session while the
prefix is still warm.
