---
name: headless-workflow
description: Orchestrate a resumable graph of headless workers with bounded concurrency and cached steps. Use when a task needs multiple dependent or parallel workers and durable orchestration beyond one headless-agent run.
---

# Headless workflow

One Python process orchestrates a swarm of `headless-agent` workers from a
short workflow script, journals every step, and lets the caller wait with a
single long tool call. It mirrors Claude Code's Workflow tool (`agent`,
`parallel`, `pipeline`, `phase`, `log`, `workflow`, `isolation="worktree"`,
resume from cache) across every headless provider. It adds native `fork`,
route fallback, structured-output repair and per-route concurrency, so a Codex
or Claude supervisor can finish dozens of bounded tasks per wave without
spending its own turns.

Derive `SKILL_DIR` from this loaded skill; `HW="$SKILL_DIR/scripts/headless-workflow.py"`.

## Explore once, fork many

When several workers need the same large context (one diff, spec or module
set), do not give each worker that context fresh. Have **one explorer read it,
then fork every worker from the explorer's session**. Each fork starts with the
explorer's full history and reads it from the provider's prompt cache at about
0.1× input price. A 15-dimension review of a large PR then pays for reading the
PR once, not 15 times, and every reviewer skips straight to its own work.

```python
explorer = await wf.agent(f"Read {target} and the code it touches. Do not review. Reply with a file map.",
                          route="sonnet", dir=root, fallback=[], label="explore")
reviews = await wf.parallel([
    (lambda d=d: wf.fork(explorer, f"Review only for {d}. JSON findings.", schema=FINDINGS,
                         require_native=True, label=f"review:{d}"))
    for d in dimensions])
```

Before writing one, read [fork-fanout](references/fork-fanout.md). It covers
the cache rules (a fork keeps the parent's route, model, effort, dir and
posture; do not override them), explorer sizing and sharding for changes
bigger than one context window, provider economics, when not to fork, and how
to confirm cache hits. `examples/fork-fanout.py` is the full
explore → review → verify → synthesize recipe.

## Write the script

```python
META = {"name": "review-batch", "description": "digest then judge"}
SCHEMA = {"type": "object", "required": ["findings"]}

async def main(wf, args):
    wf.phase("digest")
    digests = await wf.parallel([
        (lambda f=f: wf.agent(f"Digest {f} as JSON.", route="gemini", schema=SCHEMA, label=f"digest:{f}"))
        for f in args["files"]])
    verdicts = await wf.pipeline(
        [d for d in digests if d],
        lambda d, i: wf.agent(f"Judge this digest; JSON findings: {d.data}", route="glm", schema=SCHEMA, phase="judge"))
    return {"verdicts": [v.data for v in verdicts if v]}
```

`args` is the parsed JSON from `--args` (or `None`). `wf.agent()` returns an
`AgentResult`: `.ok`, `.text`, `.data` (parsed JSON when `schema` is given),
`.session_id`, `.route`, `.attempts`, `.error`; `r["k"]` and `r.get("k")`
read from `.data`. A failed step is falsy rather than an exception, so filter
results the way Claude's `.filter(Boolean)` does. Only `WorkflowError` is
fatal: an unknown route, the `--max-agents` cap, or a misconfigured step such
as `isolation` outside a git checkout. It fails the run even inside
`parallel`/`pipeline`.

- `wf.parallel` takes zero-argument callables (bind loop variables with
  `lambda x=x:`) or awaitables. Unlike the built-in, `wf.pipeline(items, s1, s2)`
  calls the first stage as `s1(item, i)`, then later stages as
  `s2(prev, item, i)`, with no barrier; a stage returning `None` drops the item.
- Inside `pipeline`/`parallel`, pass `phase=` per call rather than calling
  `wf.phase()`, which is global.
- `isolation="worktree"` runs one writer in a fresh git worktree of `dir`.
  `await wf.workflow("other.py", args)` runs another script inline, one level
  deep.
- Keep scripts deterministic: no clocks, no randomness, and variable inputs
  only through `--args`. Identical calls are fine, because the nth identical
  call has its own journal entry.

## Run and wait

```bash
python3 "$HW" run review.py --args '{"files": ["a.md", "b.md"]}' --concurrency 8
python3 "$HW" status <RUN_ID>          # steps, states, routes, sessions
python3 "$HW" result <RUN_ID>          # the JSON main() returned
python3 "$HW" run review.py --resume <RUN_ID>   # same args; unchanged steps come back cached
```

The run prints `RUN_ID`, `RUN_DIR`, and `LOG` first, then blocks until
`main()` returns. From Codex, start it as a background cell and wait on that same process with the harness's bounded wait facility.
Use the longest permitted wait, keeping required progress updates responsive;
do not relaunch work just because a tool yielded. From Claude Code, run it with
`run_in_background: true`.

## Routes

`python3 "$HW" routes` prints the effective table. Built-ins: `glm`,
`gemini`, `muse`, `kimi`, `deepseek` (cheap workers), `sonnet`, `opus`,
`luna`, `terra`, `sol` (judgment), `astra` (cautious frontier escalation), and
`pi-glm`, `pi-muse`, `pi-sol` (the
same providers through the minimal `pi` harness). Each carries harness/provider/model,
effort, posture, `max_concurrency`, a quota id for the `check-ai-quota`
preflight (exit 20, 22 or 23 skips the route), and a `fallback` list tried in order
when a dispatch fails. Add `"format": "json"` to any route when you need to
account for its token spend afterwards: without it a codex route runs in its
text default, whose stream carries no structured usage. Override or add routes in
`~/.config/headless-workflow/routes.json` or `--routes file.json` (per-name
merge), or pass a dict: `route={"harness": "kimi_code", "provider": "kimi",
"model": "k3"}`. `gemini` defaults to posture `code` because `agy` in review
posture is denied file reads.

`astra` selects `gpt-6-astra` at medium, concurrency 1, JSON output for token
accounting, and no fallback. Agents may select it without Colin's permission
when `route-ai-work` justifies its premium; do not use it for speculative bulk
fan-out. It spends the chosen account's shared OpenAI allowance at 2.5x Sol's
Standard token rates. Codex supports low/medium/high/xhigh/max plus `ultra`
automatic delegation; no none/minimal. The account-selection policy still
applies: `route="astra"` retains the active account, while
`openai_account="aether"` or the managed `astra-aether` route selects Aether.

Use a lean `pi-*` route when its limited toolset meets the task. Steps needing
MCP, delegation, or discovered skills may need another harness. Read
[context-cost](references/context-cost.md) for the measured comparison and
its limits; the small benchmark is not a guarantee for other tasks.

## Accounts, fork fallback and structured output

For a named Codex/OpenAI account, pass `openai_account="aether"` to
`wf.agent(..., route="sol")`, or put `"openai_account": "aether"` in a route
entry. Account selection is passed to both quota preflight and headless-agent;
quota caches and journal step keys distinguish accounts. A named account with
unknown quota or invalid configuration is blocked. Combined quota cannot pass
preflight. No built-in fallback selects an additional account automatically.
The managed `astra-aether`, `sol-aether`, `terra-aether`, and `luna-aether` routes are explicit
choices supplied by dev-environment. Other harness/provider pairs reject
`openai_account`.

`wf.fork()` is native on `claude_code`, `codex`, `opencode`, `pi` and
`prime-agent` routes. A fork or `resume=` step never falls back to another
route, because another provider or model cannot continue the session or share
its cache. It retries its own route once after a failure instead. If a native
fork is impossible (a non-fork route, or a route other than the parent's),
`fork()` sends a fresh agent the parent's prompt and final answer as context,
with `forked=False`; pass `require_native=True` to fail instead. A failed or
session-less parent returns a failed result. `schema=` forces JSON: fences and
prose are stripped, the object is validated, and an invalid reply is repaired
by resuming the same session (up to `retries`, default 2) before the step fails.

Full API, journal layout, route fields and built-in parity: `references/api.md`.
Proven shapes (digest→judge→verify→synthesize, loop-until-dry, issue swarms,
independent-lens verification): `references/patterns.md`.

## Test

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests"   # fake dispatcher, no providers
```
