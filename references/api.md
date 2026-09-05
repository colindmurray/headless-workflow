# headless-workflow — API reference

## Script contract

A workflow script is a Python file with:

```python
META = {"name": "short-name", "description": "one line"}   # name becomes part of RUN_ID

async def main(wf, args):
    ...
    return anything_json_serialisable
```

`args` is the value of `--args` (JSON literal or `@file`), or `None`. The
return value is written to `RUN_DIR/result.json` and printed by `result`.

Determinism: step identity is `sha256(kind, prompt, route name, overrides,
schema, parent session)`, plus the optional cell-granular fields `cell`,
`neighbors`, `evidence_digest`, and `input_identity` when given (each takes
part only when supplied, so keys computed without them are unchanged).
Anything that changes a prompt between runs (timestamps, random ids,
unordered dict iteration) defeats `--resume` caching. Put variable inputs in
`--args` and stamp outputs after the run. When the evidence behind a prompt
can be repaired without changing the prompt template, pass a deterministic
digest of that evidence as `evidence_digest` (or an equivalent immutable
`input_identity`) so the repair retires the old cache entry.

## `wf` methods

| Call | Returns | Notes |
|---|---|---|
| `await wf.agent(prompt, route="glm", schema=None, label=None, dir=None, posture=None, effort=None, model=None, fallback=None, retries=2, timeout=None, fork_from=None, resume=None, add_dirs=None, success=None, cell=None, neighbors=None, evidence_digest=None, input_identity=None)` | `AgentResult` | Dispatches one worker through `headless-agent.sh --wait` and blocks until it answers. `success` is an optional semantic success predicate over the finished result: a transport-ok reply it rejects is journaled as `failed` (never `completed`) and carries the reviewer's text/data for reporting, so `--resume` retries it. `cell` names one review cell, `neighbors` declares the neighbor cells it was judged with, and `evidence_digest`/`input_identity` pin the evidence content behind the prompt. Run one such agent per cell under `parallel()` and a resume redispatches only the failed or unrun cells. |
| `await wf.fork(parent, prompt, **agent_opts)` | `AgentResult` | Native `--fork <parent.session_id>` when the route's harness supports it and `route` is the parent's route; otherwise fresh agent with the parent's prompt and answer prepended (`forked=False`). |
| `await wf.parallel([thunk, ...])` | `list` | Barrier. Each thunk is a zero-arg callable returning an awaitable. A thunk that raises yields `None`. |
| `await wf.pipeline(items, stage1, stage2, ...)` | `list` | Per-item chains with no barrier. `stage1(item, index)`, later stages `(prev, item, index)`. A raising stage or a `None` result drops that item to `None`. |
| `wf.log(msg)` | – | Appends to `RUN_DIR/log.txt` and stderr. |
| `wf.phase(title)` | – | Groups following steps in `status`. |
| `wf.args`, `wf.routes`, `wf.run_id`, `wf.run_dir` | – | Read-only context. |

### `AgentResult`

| Field | Meaning |
|---|---|
| `ok` | step succeeded (also the truth value of the object) |
| `text` | the worker's final answer (`final.txt`) |
| `data` | parsed JSON when `schema` was given, else `None` |
| `session_id` | native session id from `RUN_DIR/session_id`; feed to `fork()` / `resume=` |
| `run_dir` | the provider run directory (stream, stderr, meta) |
| `route` | route name actually used (after fallback) |
| `model`, `effort` | model and effort sent to the dispatcher; same-route forks inherit them unless explicitly overridden |
| `attempts` | dispatches consumed, including repairs and fallbacks |
| `forked` | `True` when this result came from a native fork |
| `error` | reason when `ok` is `False` |
| `cached` | `True` when served from the journal on `--resume` |
| `cell`, `neighbors`, `evidence_digest`, `input_identity` | the cell-granular identity the step was dispatched with (`None` when unused) |

`result["key"]` and `result.get("key")` read from `data`.

### Structured output

With `schema=`, the reply is parsed by `extract_json` (first fenced JSON,
else first `{`/`[` that decodes) and checked by a small validator: `type`,
`required`, `properties`, `items`, `enum`, `minItems`/`maxItems`,
`minimum`/`maximum`. On failure the SAME session is resumed with a repair
prompt carrying the validator message and the schema; after `retries` extra
attempts the step returns `ok=False`, `error` mentioning `schema`, and no
other route is tried (a schema miss is a model-output problem, not a route
problem).

### Route fallback

A dispatch counts as failed when the dispatcher exits non-zero, the run's
`exit_code` file is non-zero, the reply is short and matches a provider-error
pattern (429, 401, rate limit, overloaded, timed out, quota, "no output
produced"), or the route's `timeout` elapses (process group killed). The next
route in `fallback` (explicit argument, else the route's table entry) is then
tried with a fresh session. Fork requests skip routes that cannot fork.

## Routes table

Each entry:

```json
"glm": {"harness": "claude_code", "provider": "zai", "model": "glm-5.3-flash",
        "effort": "high", "posture": "review", "max_concurrency": 4,
        "quota": "zai", "fallback": ["muse", "gemini"], "timeout": 1800}
```

- `quota`: `check-ai-quota --provider` id (`zai`, `gemini`, `meta`, `kimi`,
  `claude`, `openai`, `deepseek`); `null` disables preflight for that route.
  Preflight runs once per provider per run; exit 20 (exhausted) or 22
  (critically limited) blocks the route, 21 (unknown) allows it with a log line.
  `--no-preflight` or `HEADLESS_WORKFLOW_NO_PREFLIGHT=1` skips all checks.
- `max_concurrency`: per-route semaphore; `--concurrency` is the global cap.
- `openai_account`: configured account id, only on `codex/openai`; also a
  `wf.agent()` keyword. Both preflight and dispatch select it. Quota cache keys
  include account and model; named-account checks fail closed. The result's
  `openai_account` records the id (or active Codex home for unnamed routes).
  Account changes invalidate journal hits and cannot reuse a native fork.
  Session ids must be resumed in the home where they were created.
- `timeout`: seconds; passed to `agy` as `--timeout`, enforced locally for all.
- Overlay order: built-ins ← `~/.config/headless-workflow/routes.json` ←
  `--routes file` ← per-call `route={...}` dict / `posture=`, `effort=`,
  `model=`, `timeout=`, `dir=` overrides.

## Run directory

`${HEADLESS_WORKFLOW_STATE:-~/.local/state/headless-workflow}/runs/<RUN_ID>/`

| File | Content |
|---|---|
| `run.json` | name, script, args, status (`running`/`completed`/`failed`), timestamps, dispatch count |
| `journal.jsonl` | `run-started`, `phase`, `started`, `completed` (with the full result), `failed`, `run-finished` |
| `steps/<key>/prompt-N.txt`, `dispatch-N.log`, `result-N.json` | exact prompt sent, dispatcher command and output, parsed outcome per attempt |
| `result.json` | `main()` return value |
| `log.txt` | human log |

`--resume RUN_ID` reopens the same directory; steps whose key already has a
`completed` event return the cached `AgentResult` (`cached=True`) without a
dispatch. Edited prompts or new steps run live. A step that used `success=`
is completed only when transport succeeds AND the predicate accepts the
result; cached entries are re-checked against the current predicate on
resume, so adopting a predicate retries stale transport-ok failures instead
of serving them.

## Environment

| Variable | Purpose |
|---|---|
| `HEADLESS_WORKFLOW_DISPATCHER` | path to `headless-agent.sh` (auto-discovered under skillshare/claude/codex skill roots) |
| `HEADLESS_WORKFLOW_QUOTA` | path to `check-ai-quota`'s `quota.py` |
| `HEADLESS_WORKFLOW_STATE` | state root (default `~/.local/state/headless-workflow`) |
| `HEADLESS_WORKFLOW_ROUTES` | user routes overlay path |
| `HEADLESS_WORKFLOW_NO_PREFLIGHT=1` | skip quota preflight |

## CLI

```
run <script> [--args JSON|@file] [--resume RUN_ID] [--routes f] [--max-agents N] [--concurrency N] [--no-preflight] [--quiet]
status <RUN_ID>      result <RUN_ID>      list      routes [--routes f]
```

`run` exits 0 on `completed`, 1 on `failed` (unknown route, `--max-agents`
reached, script exception). `--max-agents` (default 200) counts dispatches
including repairs and fallbacks.
