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
schema, parent session)`, plus the call's occurrence number when the same
identity is called more than once in a run (the nth identical call is its own
step, so identical voters or a loop that re-issues one prompt resume one result
per call), plus the optional cell-granular fields `cell`,
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
| `await wf.agent(prompt, route="glm", schema=None, label=None, dir=None, posture=None, effort=None, model=None, fallback=None, retries=2, timeout=None, fork_from=None, resume=None, add_dirs=None, openai_account=None, success=None, cell=None, neighbors=None, evidence_digest=None, input_identity=None, phase=None, isolation=None)` | `AgentResult` | Dispatches one worker through `headless-agent.sh --wait` and blocks until it answers. `phase` groups the step in `status` without touching the global `phase()` title (use it inside `pipeline`/`parallel`). `isolation="worktree"` runs the worker in a fresh detached git worktree of the checkout holding `dir` (or the cwd), at the same subdirectory, under `RUN_DIR/worktrees/`; the worktree is removed when the worker leaves it unchanged, otherwise its path is on `result.worktree`. A removed worktree cannot be forked from. `success` is an optional semantic success predicate over the finished result: a transport-ok reply it rejects is journaled as `failed` (never `completed`) and carries the reviewer's text/data for reporting, so `--resume` retries it. `cell` names one review cell, `neighbors` declares the neighbor cells it was judged with, and `evidence_digest`/`input_identity` pin the evidence content behind the prompt. Run one such agent per cell under `parallel()` and a resume redispatches only the failed or unrun cells. |
| `await wf.fork(parent, prompt, require_native=False, **agent_opts)` | `AgentResult` | Native `--fork <parent.session_id>` when the route's harness can fork and the route is the parent's (the default: a dict-route parent's dict is reused). The child inherits the parent's `dir`, `posture`, `add_dirs`, and any model/effort override unless given, so it opens the session and hits its cached prefix. Otherwise a fresh agent receives the parent's prompt and answer prepended (`forked=False`, logged), or, with `require_native=True`, the step fails. A failed or session-less parent, a parent whose directory is gone (a removed `isolation` worktree), and `isolation` on a native fork each return a failed result. See `fork-fanout.md`. |
| `await wf.parallel([thunk, ...])` | `list` | Barrier. Each element is a zero-arg callable (sync or async) or an awaitable. One that raises yields `None`, except `WorkflowError`, which fails the run. |
| `await wf.pipeline(items, stage1, stage2, ...)` | `list` | Per-item chains with no barrier. `stage1(item, index)`, later stages `(prev, item, index)`; stages may be sync or async. A raising stage or a `None` result drops that item to `None`; `WorkflowError` fails the run. |
| `await wf.workflow(script, args=None)` | any | Runs another workflow script's `main(wf, args)` inline on this run (same journal, caps, concurrency). A relative path resolves against the calling script's directory. One nesting level. |
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
| `harness`, `provider`, `dir`, `posture`, `add_dirs` | where the session lives; `fork()` reuses them |
| `route_spec` | the route dict, when the step used one |
| `worktree` | path of a changed `isolation="worktree"` worktree, else `None` |
| `error` | reason when `ok` is `False` |
| `cached` | `True` when served from the journal on `--resume` |
| `cell`, `neighbors`, `evidence_digest`, `input_identity` | the cell-granular identity the step was dispatched with (`None` when unused) |

`result["key"]` and `result.get("key")` read from `data`. A cached result
takes the label of the call it is served to.

### Structured output

With `schema=`, the reply is parsed by `extract_json` (first fenced JSON,
else the first `{`/`[` that decodes and validates, else the first that decodes) and checked by a small validator: `type`,
`required`, `properties`, `items`, `enum`, `minItems`/`maxItems`,
`minimum`/`maximum`. On failure the SAME session is resumed with a repair
prompt carrying the validator message and the schema; after `retries` extra
attempts the step returns `ok=False`, `error` mentioning `schema`, and no
other route is tried (a schema miss is a model-output problem, not a route
problem). When the harness reported no session id, the repair re-sends the
original prompt with the repair instruction appended (re-forking from the same
parent for a fork).

### Route fallback

A dispatch counts as failed when the dispatcher exits non-zero, the run's
`exit_code` file is non-zero, the reply is short and *starts* like a provider
error ("API Error", "Error:", "rate limit", "quota exceeded", an HTTP status
such as "503 Service…", "no output produced"), or the route's `timeout` elapses
(the worker's process group is killed). The next route in `fallback` (explicit
argument, else the route's table entry) is then tried with a fresh session.

A `fork_from`/`resume` step never falls back: any other route is another
provider or model, which cannot share the session's cache and may not be able to
replay its transcript. Instead it retries its own route once after
`HEADLESS_WORKFLOW_SESSION_RETRY_DELAY` seconds (default 20), except after "No
conversation found", which will not change. The returned error is the last real
dispatch error; skip reasons (quota preflight) appear only when nothing was
dispatched.

## Routes table

Each entry:

```json
"glm": {"harness": "claude_code", "provider": "zai", "model": "glm-5.3-flash",
        "effort": "high", "posture": "review", "max_concurrency": 4,
        "quota": "zai", "fallback": ["muse", "gemini"], "timeout": 1800}
```

- `quota`: `check-ai-quota --provider` id (`zai`, `gemini`, `meta`,
  `claude`, `openai`, `deepseek`); `null` disables preflight for that route.
  Preflight runs once per (quota id, account, model) per run, off the event
  loop; exit 20 (exhausted), 22 (critically limited) or 23 blocks the route,
  and 21 (unknown) allows it with a log line. A named `openai_account` needs
  exit 0.
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

`--resume RUN_ID` reopens the same directory and reuses the run's original
`--args` unless new ones are given; steps whose key already has a
`completed` event return the cached `AgentResult` (`cached=True`) without a
dispatch. An unknown RUN_ID is refused. `run.json` keeps `started_at` and
appends each resume to `resumes`; the previous `result.json` moves to
`result.prev.json` until the resumed run completes. Fork steps journaled before
fork() began inheriting dir and posture (September 2026) have different keys and
re-run once on `--resume`. Edited prompts or new steps run live. A step that used `success=`
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
| `HEADLESS_WORKFLOW_SESSION_RETRY_DELAY` | seconds before a failed fork/resume step retries its route (default 20) |

## CLI

```
run <script> [--args JSON|@file] [--resume RUN_ID] [--routes f] [--max-agents N] [--concurrency N] [--no-preflight] [--quiet]
status <RUN_ID>      result <RUN_ID>      list      routes [--routes f]
```

`run` exits 0 on `completed`, 1 on `failed` (unknown route, `--max-agents`
reached, script exception), and 130 on `interrupted` (SIGINT, SIGTERM or
SIGHUP). An interrupt kills every in-flight worker's process group. So does a
timeout. `--max-agents` (default 200) is a hard cap on dispatches, including
repairs, retries and fallbacks. `result` refuses unless the run's status is
`completed`. A `nohup`-ignored SIGHUP stays ignored. `--routes` takes a file
path.

## Parity with Claude Code's Workflow tool

| Built-in | Here |
|---|---|
| `agent(prompt, {label, phase, schema, model, effort, isolation})` | `wf.agent(prompt, label=, phase=, schema=, model=, effort=, isolation=)`, plus `route`, `dir`, `posture`, `fallback`, `resume`, cell identity |
| returns text / object / `null` | `AgentResult`: falsy on failure; `.text` / `.data` |
| `parallel(thunks)` barrier, throw → `null` | same; also accepts awaitables |
| `pipeline(items, ...stages)`, every stage `(prev, item, i)` | stage 1 is `(item, i)`; later stages `(prev, item, i)` |
| `phase()`, `log()`, `args` | same |
| `workflow(nameOrRef, args)`, one level | `wf.workflow(path, args)`, one level |
| resume: longest unchanged prefix of calls | per-step cache: any unchanged step is served, even after an edited one |
| `Date.now()`/`Math.random()` throw | not enforced; keep scripts deterministic |
| `budget` token ceiling | none; `--max-agents` caps dispatches and each route has a `timeout` |
| `agentType` | routes and postures select the worker |
| — | `fork()`: native session branching with cached-prefix reuse |
