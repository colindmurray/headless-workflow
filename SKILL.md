---
name: headless-workflow
description: Use when one task needs many headless-agent workers at once — a fan-out over files, issues, or lanes, a digest→judge→verify→synthesize pass, forked children sharing one parent's context, or a swarm mixing providers — and the caller (Codex, Claude Code, or a shell) must wait cheaply and resume after a crash instead of babysitting each run.
---

# Headless workflow

One Python process orchestrates a swarm of `headless-agent` workers from a
short workflow script, journals every step, and lets the caller wait with a
single long tool call. It mirrors Claude Code's Workflow tool (`agent`,
`parallel`, `pipeline`) and adds `fork`, route fallback, structured-output
repair, and per-route concurrency, so a Codex supervisor can knock out dozens
of bounded tasks per wave without spending its own turns.

Derive `SKILL_DIR` from this loaded skill; `HW="$SKILL_DIR/scripts/headless-workflow.py"`.

## Write the script

```python
META = {"name": "review-batch", "description": "digest then judge"}
SCHEMA = {"type": "object", "required": ["findings"]}

async def main(wf, args):
    wf.phase("digest")
    digests = await wf.parallel([
        (lambda f=f: wf.agent(f"Digest {f} as JSON.", route="gemini", schema=SCHEMA, label=f"digest:{f}"))
        for f in args["files"]])
    wf.phase("judge")
    ctx = await wf.agent("Read every digest under ./digests; reply READY.", route="glm", label="context")
    verdicts = await wf.parallel([
        (lambda f=f: wf.fork(ctx, f"Judge only {f}; return JSON findings.", schema=SCHEMA)) for f in args["files"]])
    return {"digests": [d.data for d in digests if d], "verdicts": [v.data for v in verdicts if v]}
```

`args` is the parsed JSON from `--args` (or `None`). `wf.agent()` returns an
`AgentResult`: `.ok`, `.text`, `.data` (parsed JSON when `schema` is given),
`.session_id`, `.route`, `.attempts`, `.error`; `r["k"]` and `r.get("k")`
read from `.data`. A failed step is falsy, never an exception, so filter
results the way Claude's `.filter(Boolean)` does. `wf.parallel` takes
zero-argument callables (the `lambda x=x:` form binds loop variables), not
bare coroutines; `wf.pipeline(items, s1, s2)` calls `s1(item, i)` then
`s2(prev, item, i)` per item with no barrier. Step keys derive from prompt +
route + parent, so keep scripts deterministic: no clocks, no randomness, and
variable inputs only through `--args`.

## Run and wait

```bash
python3 "$HW" run review.py --args '{"files": ["a.md", "b.md"]}' --concurrency 8
python3 "$HW" status <RUN_ID>          # steps, states, routes, sessions
python3 "$HW" result <RUN_ID>          # the JSON main() returned
python3 "$HW" run review.py --resume <RUN_ID>   # unchanged steps come back cached
```

The run prints `RUN_ID`, `RUN_DIR`, and `LOG` first, then blocks until
`main()` returns. From Codex, start it as a background cell and make ONE
`write_stdin(chars:"", yield_time_ms: <expected minutes × 60000>)`; the
`sleep-and-wait` rule applies. From Claude Code, run it with
`run_in_background: true`.

## Routes

`python3 "$HW" routes` prints the effective table. Built-ins: `glm`,
`gemini`, `muse`, `kimi`, `deepseek` (cheap workers), `sonnet`, `opus`,
`luna`, `terra`, `sol` (judgment), and `pi-glm`, `pi-muse`, `pi-sol` (the
same providers through the minimal `pi` harness). Each carries harness/provider/model,
effort, posture, `max_concurrency`, a quota id for the `check-ai-quota`
preflight (exit 20/22 skips the route), and a `fallback` list tried in order
when a dispatch fails. Add `"format": "json"` to any route when you need to
account for its token spend afterwards: without it a codex route runs in its
text default, whose stream carries no structured usage. Override or add routes in
`~/.config/headless-workflow/routes.json` or `--routes file.json` (per-name
merge), or pass a dict: `route={"harness": "kimi_code", "provider": "kimi",
"model": "k3"}`. `gemini` defaults to posture `code` because `agy` in review
posture is denied file reads.

**Prefer a `pi-*` route for a wide fan-out whose provider supports pi.** The
same 25-agent graph — 20 parallel research steps, 4 analyses, 1 report — run
twice on `gpt-5.6-luna` at low effort, once through `codex` and once through
`pi` at `context: lean`:

| | codex | pi (lean) |
| --- | --- | --- |
| Prompt tokens, mean per worker | 34,610 | 1,528 |
| Prompt tokens, total | 865,250 | 39,728 |
| Total tokens | 871,893 | 42,313 |
| Median step | 42.0s | 11.5s |
| Wall clock | 132s | 70s |
| Steps at exit 0 | 25/25 | 26/26 |

Both produced 20 digests, 4 analyses, and a report of the same length reaching
the same recommendation — a 20x token saving and a 1.9x speedup at equal output.

The saving comes from `context: lean`, not from pi itself. Codex loads the whole
skill tree into every worker and cannot be told not to; it even warns that it
truncated skill descriptions to fit. Pi at `context: standard` costs 16,450
tokens against codex's 19,065, only 14% better. So the recommendation is
specifically **pi plus lean context**, and raising a pi route to `standard`
gives most of the advantage back.

Stay on the non-pi routes when a step needs MCP tools, sub-delegation, a richer
built-in toolset — pi has only `read`, `bash`, `edit`, `write`, `grep`, `find`,
`ls` — or a skill actually loaded into the worker.

## Fork and structured output

For a named Codex/OpenAI account, pass `openai_account="aether"` to
`wf.agent(..., route="sol")`, or put `"openai_account": "aether"` in a route
entry. Account selection is passed to both quota preflight and headless-agent;
quota caches and journal step keys distinguish accounts. A named account with
unknown quota or invalid configuration is blocked. Combined quota cannot pass
preflight. No built-in fallback selects an additional account automatically.
The managed `sol-aether`, `terra-aether`, and `luna-aether` routes are explicit
choices supplied by dev-environment. Other harness/provider pairs reject
`openai_account`.

`wf.fork(parent, prompt)` continues the parent's session on fork-capable
harnesses (`claude_code`, `codex`, `opencode`, `pi`, `prime-agent`): children share
the parent's history and cached prefix. On other harnesses it becomes a fresh
agent that receives the parent's prompt and answer as context, and
`result.forked` is `False`. `schema=` forces JSON: fences and prose are
stripped, the object is validated, and an invalid reply is repaired by
resuming the same session (up to `retries`, default 2) before the step fails.

Full API, journal layout, and route fields: `references/api.md`. Proven
shapes (digest→judge→verify→synthesize, shared-context fork fan-out,
loop-until-dry, issue swarms): `references/patterns.md`.

## Test

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests"   # fake dispatcher, no providers
```
