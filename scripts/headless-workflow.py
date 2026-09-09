#!/usr/bin/env python3
"""headless-workflow — run a Python workflow script over many headless-agent
workers with a durable journal, resume, fork, route fallback, structured
output repair, and concurrency limits.

Usage:
  headless-workflow.py run <script.py> [--args JSON|@file] [--resume RUN_ID]
                          [--routes routes.json] [--max-agents N] [--concurrency N]
                          [--no-preflight] [--quiet]
  headless-workflow.py status <RUN_ID>
  headless-workflow.py result <RUN_ID>
  headless-workflow.py list
  headless-workflow.py routes [--routes routes.json]

A workflow script defines META = {"name": ..., "description": ...} and
`async def main(wf, args)`; `wf` exposes agent(), fork(), parallel(),
pipeline(), log(), phase(). See references/api.md.

Stdlib only; Python 3.9+.
"""
import argparse
import asyncio
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import uuid

# --------------------------------------------------------------------------
# Locations and defaults
# --------------------------------------------------------------------------

STATE_ROOT = os.path.expanduser(os.environ.get("HEADLESS_WORKFLOW_STATE", "~/.local/state/headless-workflow"))
CONFIG_ROUTES = os.path.expanduser(os.environ.get("HEADLESS_WORKFLOW_ROUTES", "~/.config/headless-workflow/routes.json"))

DEFAULT_ROUTES = {
    # name: harness/provider/model; quota id maps to check-ai-quota --provider
    "glm":      {"harness": "claude_code", "provider": "zai",       "model": "glm-5.3-flash",              "effort": "high", "posture": "review", "max_concurrency": 4, "quota": "zai",      "fallback": ["muse", "gemini"], "timeout": 1800},
    "gemini":   {"harness": "agy",         "provider": "google",    "model": "gemini-3.8-flash",           "effort": "high", "posture": "code",   "max_concurrency": 8, "quota": "gemini",   "fallback": ["muse", "glm"],    "timeout": 1500},
    "muse":     {"harness": "muse",        "provider": "meta",      "model": "muse-spark-1.3-contributor", "effort": "high", "posture": "review", "max_concurrency": 6, "quota": "meta",     "fallback": ["glm", "gemini"],  "timeout": 1800},
    "kimi":     {"harness": "kimi_code",   "provider": "kimi",      "model": "k3",                         "effort": "max",  "posture": "review", "max_concurrency": 3, "quota": "kimi",     "fallback": ["glm", "muse"],    "timeout": 1800},
    "deepseek": {"harness": "claude_code", "provider": "deepseek",  "model": "deepseek-v4-flash",          "effort": "high", "posture": "review", "max_concurrency": 4, "quota": "deepseek", "fallback": ["glm", "muse"],    "timeout": 1800},
    "sonnet":   {"harness": "claude_code", "provider": "anthropic", "model": "sonnet",                     "effort": "high", "posture": "review", "max_concurrency": 4, "quota": "claude",   "fallback": [],                 "timeout": 1800},
    "opus":     {"harness": "claude_code", "provider": "anthropic", "model": "opus",                       "effort": "high", "posture": "review", "max_concurrency": 3, "quota": "claude",   "fallback": [],                 "timeout": 2400},
    "luna":     {"harness": "codex",       "provider": "openai",    "model": "gpt-5.6-luna",               "effort": "high", "posture": "review", "max_concurrency": 3, "quota": "openai",   "fallback": [],                 "timeout": 1800},
    "terra":    {"harness": "codex",       "provider": "openai",    "model": "gpt-5.6-terra",              "effort": "high", "posture": "review", "max_concurrency": 2, "quota": "openai",   "fallback": [],                 "timeout": 2400},
    "sol":      {"harness": "codex",       "provider": "openai",    "model": "gpt-5.6-sol",                "effort": "high", "posture": "review", "max_concurrency": 2, "quota": "openai",   "fallback": [],                 "timeout": 2400},
    "astra":    {"harness": "codex", "provider": "openai", "model": "gpt-6-astra", "effort": "medium", "posture": "review", "max_concurrency": 1, "quota": "openai", "fallback": [], "format": "json", "timeout": 2400},
    # pi routes: the same providers through a minimal harness. Pi has no MCP,
    # no subagents and no permission prompts, so its system prompt is ~1.3k
    # tokens against Claude Code's or Codex's much larger one, and it reaches
    # its first event in ~0.3s. Prefer these for wide fan-outs of small bounded
    # steps where the per-worker prompt tax dominates. `context: lean` keeps
    # skills and context files out of that prompt.
    # `format: "json"` is available on any route and makes the provider emit a
    # structured event stream, which is the only way to account for a run's real
    # token spend afterwards. It is not the default because changing a live
    # campaign's stream format mid-flight is not worth the churn.
    "pi-glm":   {"harness": "pi", "provider": "zai",          "model": "glm-5.3-flash",              "effort": "high", "posture": "review", "context": "lean", "max_concurrency": 6, "quota": "zai",    "fallback": ["glm", "pi-muse"],  "timeout": 1800},
    "pi-muse":  {"harness": "pi", "provider": "meta",         "model": "muse-spark-1.3-contributor", "effort": "high", "posture": "review", "context": "lean", "max_concurrency": 6, "quota": "meta",   "fallback": ["muse", "pi-glm"],  "timeout": 1800},
    "pi-sol":   {"harness": "pi", "provider": "openai-codex", "model": "gpt-5.6-sol",                "effort": "high", "posture": "review", "context": "lean", "max_concurrency": 2, "quota": "openai", "fallback": ["sol"],             "timeout": 2400},
}

FORK_HARNESSES = {"claude_code", "codex", "opencode", "pi", "prime-agent"}
RETRYABLE_PATTERNS = ("429", "rate limit", "Rate limit", "overloaded", "503", "502", "timed out", "timeout", "401", "token expired", "quota", "exhausted", "no output produced", "no response content")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_dispatcher():
    env = os.environ.get("HEADLESS_WORKFLOW_DISPATCHER")
    if env:
        return env
    candidates = [
        "~/.config/skillshare/skills/headless-agent/scripts/headless-agent.sh",
        "~/.claude/skills/headless-agent/scripts/headless-agent.sh",
        "~/.codex/skills/headless-agent/scripts/headless-agent.sh",
        "~/Projects/custom-skills/headless-agent/scripts/headless-agent.sh",
    ]
    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.exists(p):
            return p
    raise SystemExit("headless-workflow: cannot find headless-agent.sh; set HEADLESS_WORKFLOW_DISPATCHER")


def find_quota_script():
    env = os.environ.get("HEADLESS_WORKFLOW_QUOTA")
    if env:
        return env
    for c in ["~/.config/skillshare/skills/check-ai-quota/scripts/quota.py",
              "~/.claude/skills/check-ai-quota/scripts/quota.py",
              "~/.codex/skills/check-ai-quota/scripts/quota.py",
              "~/Projects/custom-skills/check-ai-quota/scripts/quota.py"]:
        p = os.path.expanduser(c)
        if os.path.exists(p):
            return p
    return None


def load_routes(path):
    """Built-in routes, overlaid by ~/.config/headless-workflow/routes.json,
    overlaid by an explicit --routes file. Overlays merge per route name."""
    routes = copy.deepcopy(DEFAULT_ROUTES)
    for p in [CONFIG_ROUTES, path]:
        if p and os.path.exists(os.path.expanduser(p)):
            with open(os.path.expanduser(p)) as fh:
                extra = json.load(fh)
            for name, spec in extra.items():
                base = routes.get(name, {})
                base.update(spec)
                routes[name] = base
    return routes


def route_supports_fork(route):
    return route.get("harness") in FORK_HARNESSES


def account_scope(spec):
    if spec.get("harness") != "codex" or spec.get("provider") != "openai":
        return None
    return spec.get("openai_account") or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


# --------------------------------------------------------------------------
# JSON extraction and a small schema validator
# --------------------------------------------------------------------------

def extract_json(text):
    """Return the first JSON object/array found in text, or None."""
    if text is None:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\[{].*?)\s*```", t, re.S)
    candidates = [m.group(1)] if m else []
    candidates.append(t)
    dec = json.JSONDecoder()
    for cand in candidates:
        for i, ch in enumerate(cand):
            if ch in "{[":
                try:
                    obj, _ = dec.raw_decode(cand[i:])
                    return obj
                except json.JSONDecodeError:
                    continue
    return None


_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}


def validate_schema(value, schema, path="$"):
    """Minimal JSON-schema check: type, required, properties, items, enum,
    minItems/maxItems, minimum/maximum. Returns None or an error string."""
    if not isinstance(schema, dict):
        return None
    typ = schema.get("type")
    if typ:
        types = typ if isinstance(typ, list) else [typ]
        ok = False
        for tname in types:
            py = _TYPES.get(tname)
            if py is None:
                ok = True
            elif tname == "integer":
                ok = ok or (isinstance(value, int) and not isinstance(value, bool))
            elif tname == "number":
                ok = ok or (isinstance(value, (int, float)) and not isinstance(value, bool))
            else:
                ok = ok or isinstance(value, py)
        if not ok:
            return f"{path}: type mismatch, expected {typ}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: enum mismatch, expected one of {schema['enum']}"
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                return f"{path}: required key missing: {req}"
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                err = validate_schema(value[k], sub, f"{path}.{k}")
                if err:
                    return err
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path}: minItems {schema['minItems']}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path}: maxItems {schema['maxItems']}"
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(value):
                err = validate_schema(v, items, f"{path}[{i}]")
                if err:
                    return f"items: {err}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: minimum {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: maximum {schema['maximum']}"
    return None


def step_key(kind, prompt, route_name, overrides, schema, parent, cell=None, neighbors=None,
             evidence_digest=None, input_identity=None):
    """Stable identity of one agent step; the journal caches results by it.

    The trailing cell-granular fields take part in the identity only when
    given, so keys computed without them are unchanged: `cell` names one
    review cell, `neighbors` declares the neighbor cells it was judged with,
    and `evidence_digest`/`input_identity` carry a deterministic digest of
    the (possibly repaired) evidence content behind the prompt. Changing any
    supplied field retires the old cache entry and forces a fresh dispatch.
    """
    payload = {"kind": kind, "prompt": prompt, "route": route_name, "overrides": overrides or {},
               "schema": schema, "parent": parent}
    if cell is not None:
        payload["cell"] = cell
    if neighbors is not None:
        try:
            payload["neighbors"] = sorted(neighbors, key=repr) if isinstance(neighbors, (list, tuple, set)) else neighbors
        except TypeError:
            payload["neighbors"] = list(neighbors)
    if evidence_digest is not None:
        payload["evidence_digest"] = evidence_digest
    if input_identity is not None:
        payload["input_identity"] = input_identity
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# Results and journal
# --------------------------------------------------------------------------

class AgentResult:
    """What agent()/fork() return. Truthy when the step succeeded."""

    def __init__(self, **kw):
        self.ok = kw.get("ok", False)
        self.text = kw.get("text")
        self.data = kw.get("data")
        self.session_id = kw.get("session_id")
        self.run_dir = kw.get("run_dir")
        self.route = kw.get("route")
        self.label = kw.get("label")
        self.key = kw.get("key")
        self.forked = kw.get("forked", False)
        self.attempts = kw.get("attempts", 0)
        self.error = kw.get("error")
        self.prompt = kw.get("prompt")
        self.cached = kw.get("cached", False)
        self.openai_account = kw.get("openai_account")
        self.model = kw.get("model")
        self.effort = kw.get("effort")
        self.cell = kw.get("cell")
        self.neighbors = kw.get("neighbors")
        self.evidence_digest = kw.get("evidence_digest")
        self.input_identity = kw.get("input_identity")

    def __bool__(self):
        return bool(self.ok)

    def __getitem__(self, item):
        if isinstance(self.data, dict):
            return self.data[item]
        raise KeyError(item)

    def get(self, item, default=None):
        if isinstance(self.data, dict):
            return self.data.get(item, default)
        return default

    def to_dict(self):
        return {k: getattr(self, k) for k in ("ok", "text", "data", "session_id", "run_dir", "route", "label", "key", "forked", "attempts", "error", "prompt", "cached", "openai_account", "model", "effort",
                                              "cell", "neighbors", "evidence_digest", "input_identity")}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def valid_cache_key(key):
    return isinstance(key, str) and len(key) > 0


GENERIC_EVENT_TYPES = frozenset((
    "phase", "run-started", "run-finished", "started", "completed", "failed",
))


class Journal:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.path = os.path.join(run_dir, "journal.jsonl")
        self.cache = {}
        self.steps = {}
        self._cell_state = {}
        self._cell_failures = {}
        self._cache_sequence = {}
        self._sequence = 0
        if os.path.exists(self.path):
            with open(self.path) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if self._ingest(e, allow_cache=True,
                                    sequence=self._sequence + 1):
                        self._sequence += 1

    def write(self, event):
        event = dict(event)
        event["at"] = now_iso()
        with open(self.path, "a") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self._ingest(event, allow_cache=False, sequence=self._sequence + 1):
            self._sequence += 1

    def _ingest(self, event, allow_cache, sequence):
        """Classify, validate, then project -- atomically.

        The event kind is classified first: anything claiming the
        structured cell lifecycle -- exact type `cell-state`, or a top-level
        `cell`/`generation` marker -- is fully validated as a non-step
        event and can never fall through into the generic step table, even
        with a valid key. A `completed` record needs a non-empty string key
        and a mapping-shaped result before it may touch the cache or the
        step table. Other string-typed records need a non-empty string key
        for the step table. Anything else -- missing, null, or numeric
        types, missing result, invalid or unhashable keys, non-mapping
        results, malformed duplicates -- is ignored, so it can never crash
        recovery, smuggle `key=poison` into the steps, or overwrite an
        established good entry. Returns True exactly when a projection was
        applied, so callers advance the logical `_sequence` only for
        accepted events. Shared by file replay and live writes so both
        observe identical state. `allow_cache` stays replay-only: live
        writes never populate the cache, preserving the rule that only a
        fresh `--resume` replay serves cached steps.
        """
        if not isinstance(event, dict):
            return False
        kind = event.get("type")
        if kind == "cell-state" or "cell" in event or "generation" in event:
            return self._note_cell_state(event, sequence)
        if not isinstance(kind, str) or kind not in GENERIC_EVENT_TYPES:
            return False
        key = event.get("key")
        if kind in ("phase", "run-started", "run-finished"):
            if "key" not in event:
                return True
            if valid_cache_key(key):
                self.steps[key] = event
                return True
            return False
        if kind == "completed":
            result = event.get("result")
            if not (valid_cache_key(key) and isinstance(result, dict)):
                return False
            if allow_cache:
                self.cache[key] = result
                self._cache_sequence[key] = sequence
            self.steps[key] = event
            return True
        if valid_cache_key(key):
            self.steps[key] = event
            return True
        return False

    def _note_cell_state(self, event, sequence):
        """Track the latest structured cell lifecycle per explicit cell id.

        A `cell-state` record carries `cell`, `state` ("failed"/"passed"),
        and an explicitly present monotonic `generation` -- an int that is
        not a bool and is >= 0, with explicit zero accepted. Acceptance is
        one ordering decision shared by every projection: only a generation
        strictly newer than the established one updates `_cell_state`, and
        only that same accepted record may update `_cell_failures` -- equal,
        lower, delayed, duplicate, or malformed events update neither
        projection, failure ordering, cache eligibility, nor the logical
        sequence. Failure generations retain their journal sequence,
        allowing cache eligibility to compare a completion with the failure
        that followed it even after a later clear. Anything else -- missing
        or malformed generations, non-string or empty ids, unknown states,
        or malformed metadata -- is ignored safely and can never crash on
        unhashable values. Returns True exactly when the record was
        accepted. Updated on every live write as well as on replay, so a
        failure recorded mid-run is visible to later serve checks in the
        same Journal instance.
        """
        if event.get("type") != "cell-state":
            return False
        cell = event.get("cell")
        state = event.get("state")
        if "generation" not in event:
            return False
        generation = event.get("generation")
        if not (isinstance(cell, str) and cell):
            return False
        if state not in ("failed", "passed"):
            return False
        if (not isinstance(generation, int) or isinstance(generation, bool)
                or generation < 0):
            return False
        if "label" in event and not isinstance(event["label"], str):
            return False
        if "reason" in event and not isinstance(event["reason"], str):
            return False
        if ("evidence_state" in event
                and event["evidence_state"] not in ("ok", "absent", "unreadable")):
            return False
        current = self._cell_state.get(cell)
        if current is not None and generation <= current[1]:
            return False
        self._cell_state[cell] = (state, generation)
        if state == "failed":
            self._cell_failures[cell] = (generation, sequence)
        return True

    def cache_stale(self, cached, key=None):
        """True when a cached completion predates a later cell failure.

        A cached result for a cell is stale when its journal completion
        sequence is not after the latest accepted failure sequence. This
        remains true after a later clear: an older completion cannot satisfy
        restored evidence, while a fresh completion earned after the failure
        is immediately eligible. Results without a cell id, and generic
        callers without sequence metadata, retain the legacy behavior.
        """
        if not isinstance(cached, dict):
            return True
        cell = cached.get("cell")
        if not (isinstance(cell, str) and cell):
            return False
        failure = self._cell_failures.get(cell)
        if failure is None:
            return False
        completion = self._cache_sequence.get(key) if valid_cache_key(key) else None
        if completion is not None:
            return completion <= failure[1]
        current = self._cell_state.get(cell)
        return current is not None and current[0] == "failed"


# --------------------------------------------------------------------------
# The workflow context
# --------------------------------------------------------------------------

class WorkflowError(Exception):
    pass


class Workflow:
    def __init__(self, run_id, run_dir, routes, dispatcher, max_agents, global_concurrency, preflight, quiet, args):
        self.run_id = run_id
        self.run_dir = run_dir
        self.routes = routes
        self.dispatcher = dispatcher
        self.max_agents = max_agents
        self.preflight = preflight
        self.quiet = quiet
        self.args = args
        self.journal = Journal(run_dir)
        # Semaphores are created lazily inside the running loop: on Python 3.9
        # a Semaphore built before asyncio.run() binds to a different loop.
        self.global_concurrency = global_concurrency
        self._global_sem = None
        self.route_sems = {}
        self.dispatched = 0
        self.phase_title = None
        self.quota_cache = {}
        self.step_counter = 0
        self.log_path = os.path.join(run_dir, "log.txt")

    # ----- user-facing helpers -----
    def log(self, message):
        line = f"[{now_iso()}] {message}"
        with open(self.log_path, "a") as fh:
            fh.write(line + "\n")
        if not self.quiet:
            print(line, file=sys.stderr, flush=True)

    def phase(self, title):
        self.phase_title = title
        self.journal.write({"type": "phase", "title": title})
        self.log(f"phase: {title}")

    async def parallel(self, thunks):
        async def guard(t):
            try:
                return await t()
            except Exception as e:  # noqa: BLE001 - a failing thunk is a null result, never a run failure
                self.log(f"parallel: thunk failed: {e}")
                return None
        return list(await asyncio.gather(*[guard(t) for t in thunks]))

    async def pipeline(self, items, *stages):
        async def chain(item, index):
            prev = None
            for si, stage in enumerate(stages):
                try:
                    if si == 0:
                        prev = await stage(item, index)
                    else:
                        prev = await stage(prev, item, index)
                except Exception as e:  # noqa: BLE001
                    self.log(f"pipeline: item {index} stage {si} failed: {e}")
                    return None
                if prev is None:
                    return None
            return prev
        return list(await asyncio.gather(*[chain(it, i) for i, it in enumerate(items)]))

    async def fork(self, parent, prompt, **opts):
        """Continue from a finished agent's session. Native fork on
        fork-capable harnesses; otherwise a fresh agent that receives the
        parent's prompt and answer as context (result.forked is False)."""
        if not isinstance(parent, AgentResult) or not parent.session_id:
            raise WorkflowError("fork() needs a completed AgentResult with a session_id")
        route_name = opts.pop("route", parent.route)
        if route_name == parent.route:
            if parent.model and "model" not in opts:
                opts["model"] = parent.model
            if parent.effort and opts.get("model") == parent.model and "effort" not in opts:
                opts["effort"] = parent.effort
        if route_name == parent.route and "openai_account" not in opts and parent.openai_account and "/" not in parent.openai_account:
            opts["openai_account"] = parent.openai_account
        route = self._resolve_route(route_name, opts)
        if route_supports_fork(route) and parent.route == route_name and parent.openai_account == account_scope(route):
            return await self.agent(prompt, route=route_name, fork_from=parent, **opts)
        ctx = textwrap.dedent(f"""
        CONTEXT FROM A PREVIOUS AGENT (session {parent.session_id}, route {parent.route}); treat it as already-established background:
        --- previous prompt ---
        {parent.prompt or ''}
        --- previous answer ---
        {parent.text or ''}
        --- end context ---

        """)
        res = await self.agent(ctx + prompt, route=route_name, **opts)
        res.forked = False
        return res

    async def agent(self, prompt, route="glm", schema=None, label=None, dir=None, posture=None, effort=None,
                    model=None, fallback=None, retries=2, timeout=None, fork_from=None, resume=None, files=None, add_dirs=None, openai_account=None,
                    success=None, cell=None, neighbors=None, evidence_digest=None, input_identity=None):
        """Dispatch one headless worker and wait for its answer.

        Cell-granular resume (all optional, all backwards compatible): `success`
        is a semantic success predicate over the finished AgentResult — a
        transport-ok reply it rejects is journaled as failed, never completed,
        so `--resume` retries it. `cell` names one review cell, `neighbors`
        declares the neighbor cells it was judged with, and
        `evidence_digest`/`input_identity` carry a deterministic digest of the
        evidence content behind the prompt; every supplied field takes part in
        the step identity, so repaired evidence retires the old cache entry.
        Run one such agent per cell under `parallel()` and a resume
        redispatches only the failed or unrun cells. A cached completion
        older than the cell's latest structured failure is never served.
        """
        overrides = {k: v for k, v in {"dir": dir, "posture": posture, "effort": effort, "model": model, "timeout": timeout, "files": files, "add_dirs": add_dirs, "openai_account": openai_account}.items() if v is not None}
        route_name = route if isinstance(route, str) else "custom:" + hashlib.sha1(json.dumps(route, sort_keys=True).encode()).hexdigest()[:8]
        parent = fork_from.session_id if fork_from else (resume or None)
        initial_spec = self._resolve_route(route, overrides)
        if fork_from and fork_from.openai_account != account_scope(initial_spec):
            raise WorkflowError("native fork requires the parent's OpenAI account")
        cache_overrides = dict(overrides, account_scope=account_scope(initial_spec))
        neighbors_norm = None
        if neighbors is not None:
            neighbors_norm = sorted(neighbors, key=repr) if isinstance(neighbors, (list, tuple, set)) else neighbors
        key = step_key("agent", prompt, route_name, cache_overrides, schema, parent, cell=cell,
                       neighbors=neighbors_norm, evidence_digest=evidence_digest, input_identity=input_identity)
        self.step_counter += 1
        label = label or f"step-{self.step_counter}"
        cell_fields = {"cell": cell, "neighbors": neighbors_norm, "evidence_digest": evidence_digest, "input_identity": input_identity}
        cached = self.journal.cache.get(key)
        if cached:
            if self.journal.cache_stale(cached, key):
                self.log(f"{label}: cached result predates a later cell failure; redispatching")
            else:
                r = AgentResult.from_dict(cached)
                if success is not None and not self._semantic_ok(success, r, label):
                    self.log(f"{label}: cached result fails the semantic success predicate; redispatching")
                else:
                    self.log(f"cached: {label}")
                    r.cached = True
                    return r
        if fallback is None:
            fallback = list(self._resolve_route(route, overrides).get("fallback", [])) if isinstance(route, str) else []
        attempt_routes = [route] + [r for r in fallback if r != route]
        self.journal.write({"type": "started", "key": key, "label": label, "route": route_name, "phase": self.phase_title, "prompt_head": prompt[:200]})
        last_error = None
        attempts = 0
        semantic_res = None
        for rname in attempt_routes:
            spec = self._resolve_route(rname, overrides)
            rlabel = rname if isinstance(rname, str) else route_name
            if (fork_from or resume) and account_scope(spec) != account_scope(initial_spec):
                last_error = f"route {rlabel} uses a different account from the session"
                continue
            if fork_from and not route_supports_fork(spec):
                last_error = f"route {rlabel} cannot fork"
                continue
            if self.preflight and not self._quota_ok(spec):
                last_error = f"route {rlabel} blocked by quota preflight"
                self.log(f"{label}: {last_error}")
                continue
            res, err = await self._run_with_repair(prompt, spec, rlabel, label, schema, retries, fork_from, resume, key)
            attempts += res.attempts if res is not None else 1
            if res is not None and res.ok:
                for k, v in cell_fields.items():
                    setattr(res, k, v)
                if success is not None and not self._semantic_ok(success, res, label):
                    last_error = "semantic failure: the success predicate rejected a transport-ok result"
                    self.log(f"{label}: {last_error}")
                    res.ok = False
                    res.error = last_error
                    res.attempts = attempts
                    res.key = key
                    res.prompt = prompt
                    # a semantic failure is a content problem, not a route
                    # problem: do not burn other routes, like a schema failure
                    semantic_res = res
                    break
                res.attempts = attempts
                res.key = key
                res.prompt = prompt
                self.journal.write({"type": "completed", "key": key, "label": label, "route": rlabel, "result": res.to_dict()})
                return res
            last_error = err or (res.error if res is not None else "unknown")
            self.log(f"{label}: route {rlabel} failed: {last_error}")
            if res is not None and res.error and "schema" in res.error:
                # a schema failure is a model-output problem, not a route problem: do not burn other routes
                break
        self.journal.write({"type": "failed", "key": key, "label": label, "route": route_name, "error": last_error})
        if semantic_res is not None:
            # keep the worker's text/data/session on the returned result so
            # the caller can report what the reviewer actually said
            return semantic_res
        failed = AgentResult(ok=False, error=last_error, route=route_name, label=label, key=key, attempts=attempts, prompt=prompt, **cell_fields)
        return failed

    # ----- internals -----
    def _resolve_route(self, route, overrides):
        if isinstance(route, dict):
            spec = dict(route)
            spec.setdefault("max_concurrency", 4)
            spec.setdefault("fallback", [])
        else:
            if route not in self.routes:
                raise WorkflowError(f"unknown route '{route}'; known: {', '.join(sorted(self.routes))}")
            spec = dict(self.routes[route])
        for k in ("posture", "effort", "model", "timeout", "dir", "openai_account"):
            if overrides.get(k) is not None:
                spec[k] = overrides[k]
        spec["_files"] = overrides.get("files")
        spec["_add_dirs"] = overrides.get("add_dirs")
        if spec.get("openai_account") and (spec.get("harness"), spec.get("provider")) != ("codex", "openai"):
            raise WorkflowError("openai_account requires a codex/openai route")
        return spec

    @property
    def global_sem(self):
        if self._global_sem is None:
            self._global_sem = asyncio.Semaphore(self.global_concurrency)
        return self._global_sem

    def _sem_for(self, spec, rlabel):
        if rlabel not in self.route_sems:
            self.route_sems[rlabel] = asyncio.Semaphore(int(spec.get("max_concurrency", 4)))
        return self.route_sems[rlabel]

    def _quota_ok(self, spec):
        qid = spec.get("quota")
        if not qid:
            return True
        cache_key = (qid, account_scope(spec), spec.get("model"))
        if cache_key in self.quota_cache:
            return self.quota_cache[cache_key]
        script = find_quota_script()
        if not script:
            return not bool(spec.get("openai_account"))
        cmd = [sys.executable, script, "--provider", qid, "--format", "json", "--preflight", "--task-size", "small"]
        if spec.get("model"):
            cmd += ["--model", spec["model"]]
        if spec.get("openai_account"):
            cmd += ["--openai-account", spec["openai_account"], "--strict-unknown"]
        try:
            proc = subprocess.run(cmd,
                                  capture_output=True, text=True, timeout=120)
            code = proc.returncode
        except Exception as e:  # noqa: BLE001
            self.log(f"quota preflight for {cache_key} errored ({e})")
            return not bool(spec.get("openai_account"))
        # 20 exhausted / 22 critically limited block; 21 unknown allows with a note
        ok = code == 0 if spec.get("openai_account") else code not in (20, 22, 23)
        if code == 21:
            self.log(f"quota for {qid} unknown; proceeding")
        elif not ok:
            self.log(f"quota preflight blocks {qid} (exit {code})")
        self.quota_cache[cache_key] = ok
        return ok

    def _semantic_ok(self, success, result, label):
        """Evaluate the caller's semantic success predicate. A raising
        predicate is a failure, never a pass: surfacing it as failed retries
        the cell instead of caching a verdict nobody vouched for."""
        try:
            return bool(success(result))
        except Exception as e:  # noqa: BLE001
            self.log(f"{label}: success predicate raised {e!r}; treating as semantic failure")
            return False

    async def _run_with_repair(self, prompt, spec, rlabel, label, schema, retries, fork_from, resume, key):
        """Dispatch, then on invalid structured output resume the same session
        with a repair prompt up to `retries` times."""
        attempts = 0
        session = resume
        fork_id = fork_from.session_id if fork_from else None
        current_prompt = prompt
        last = None
        while True:
            attempts += 1
            text, session_id, run_dir, code, err = await self._dispatch(current_prompt, spec, rlabel, label, key, fork_id=fork_id, resume_id=session)
            fork_id = None
            if code != 0 or err:
                msg = err or f"exit {code}: {(text or '')[:300]}"
                return AgentResult(ok=False, error=msg, route=rlabel, label=label, attempts=attempts, session_id=session_id, run_dir=run_dir, text=text), msg
            last = AgentResult(ok=True, text=text, session_id=session_id, run_dir=run_dir, route=rlabel, label=label,
                               attempts=attempts, forked=bool(fork_from), openai_account=account_scope(spec),
                               model=spec.get("model"), effort=spec.get("effort"))
            if schema is None:
                return last, None
            data = extract_json(text)
            verr = validate_schema(data, schema) if data is not None else "no JSON object found in the reply"
            if verr is None:
                last.data = data
                return last, None
            if attempts > retries:
                last.ok = False
                last.error = f"structured output failed schema after {attempts} attempts: {verr}"
                return last, last.error
            session = session_id
            current_prompt = (f"Your previous reply did not satisfy the required output contract ({verr}). "
                              f"Reply again with ONLY a JSON value matching this schema, no prose and no code fences:\n{json.dumps(schema)}")
            self.log(f"{label}: repairing structured output (attempt {attempts + 1})")

    async def _dispatch(self, prompt, spec, rlabel, label, key, fork_id=None, resume_id=None):
        if self.max_agents and self.dispatched >= self.max_agents:
            raise WorkflowError(f"--max-agents {self.max_agents} reached before dispatching '{label}'")
        step_dir = os.path.join(self.run_dir, "steps", key)
        os.makedirs(step_dir, exist_ok=True)
        n = len([f for f in os.listdir(step_dir) if f.startswith("prompt")]) + 1
        prompt_path = os.path.join(step_dir, f"prompt-{n}.txt")
        with open(prompt_path, "w") as fh:
            fh.write(prompt)
        cmd = ["bash", self.dispatcher, "--harness", spec["harness"], "--provider", spec["provider"], "--model", spec["model"],
               "--posture", spec.get("posture", "review"), "--dir", os.path.abspath(spec.get("dir") or os.getcwd()),
               "--prompt-file", prompt_path, "--label", label[:80], "--wait"]
        if spec.get("effort"):
            cmd += ["--effort", spec["effort"]]
        if spec.get("openai_account"):
            cmd += ["--openai-account", spec["openai_account"]]
        if spec.get("harness") == "agy" and spec.get("timeout"):
            cmd += ["--timeout", f"{int(spec['timeout'])}s"]
        if spec.get("harness") == "pi" and spec.get("context"):
            cmd += ["--context", spec["context"]]
        if spec.get("format"):
            cmd += ["--format", spec["format"]]
        for d in (spec.get("_add_dirs") or []):
            cmd += ["--add-dir", d]
        if fork_id:
            cmd += ["--fork", fork_id]
        elif resume_id:
            cmd += ["--resume", resume_id]
        sem = self._sem_for(spec, rlabel)
        async with self.global_sem:
            async with sem:
                self.dispatched += 1
                self.log(f"dispatch {self.dispatched}: {label} -> {rlabel} ({spec['harness']}/{spec['model']})" + (f" fork={fork_id}" if fork_id else "") + (f" resume={resume_id}" if resume_id else ""))
                with open(os.path.join(step_dir, f"dispatch-{n}.log"), "a") as lg:
                    lg.write(" ".join(shlex.quote(c) for c in cmd) + "\n")
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
                try:
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(spec.get("timeout") or 1800) + 60)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except Exception:  # noqa: BLE001
                        pass
                    return None, None, None, 124, f"timed out after {spec.get('timeout')}s"
        out = out_b.decode("utf-8", "replace")
        errtxt = err_b.decode("utf-8", "replace")
        with open(os.path.join(step_dir, f"dispatch-{n}.log"), "a") as lg:
            lg.write(out + "\n--- stderr ---\n" + errtxt + "\n")
        run_dir = None
        m = re.search(r"^RUN_DIR\s*:\s*(\S+)", out, re.M)
        if m:
            run_dir = m.group(1)
        text = None
        if run_dir and os.path.exists(os.path.join(run_dir, "final.txt")):
            with open(os.path.join(run_dir, "final.txt")) as fh:
                text = fh.read().strip()
        else:
            m2 = re.search(r"^--- FINAL \(exit (\d+)\) ---\n(.*)\Z", out, re.S | re.M)
            if m2:
                text = m2.group(2).strip()
        session_id = None
        if run_dir and os.path.exists(os.path.join(run_dir, "session_id")):
            with open(os.path.join(run_dir, "session_id")) as fh:
                session_id = fh.read().strip() or None
        code = proc.returncode
        if run_dir and os.path.exists(os.path.join(run_dir, "exit_code")):
            try:
                with open(os.path.join(run_dir, "exit_code")) as fh:
                    code = int(fh.read().strip() or code)
            except ValueError:
                pass
        err = None
        if code != 0:
            err = f"exit {code}: {(text or errtxt or out)[-300:].strip()}"
        elif text is not None and any(p in text for p in RETRYABLE_PATTERNS) and len(text) < 400:
            err = f"provider error text: {text[:300]}"
        with open(os.path.join(step_dir, f"result-{n}.json"), "w") as fh:
            json.dump({"run_dir": run_dir, "session_id": session_id, "exit": code, "error": err, "text": text}, fh, ensure_ascii=False, indent=1)
        return text, session_id, run_dir, code, err


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_script(path):
    spec = importlib.util.spec_from_file_location("hw_user_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "main"):
        raise SystemExit("workflow script must define `async def main(wf, args)`")
    meta = getattr(mod, "META", {"name": os.path.basename(path), "description": ""})
    return mod, meta


def parse_args_value(raw):
    if raw is None:
        return None
    if raw.startswith("@"):
        with open(raw[1:]) as fh:
            return json.load(fh)
    return json.loads(raw)


def run_dir_for(run_id):
    return os.path.join(STATE_ROOT, "runs", run_id)


def write_run_meta(run_dir, **fields):
    p = os.path.join(run_dir, "run.json")
    meta = {}
    if os.path.exists(p):
        with open(p) as fh:
            meta = json.load(fh)
    meta.update(fields)
    with open(p, "w") as fh:
        json.dump(meta, fh, indent=1, ensure_ascii=False)
    return meta


def cmd_run(ns):
    mod, meta = load_script(os.path.abspath(ns.script))
    run_id = ns.resume or (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]+", "-", meta.get("name", "wf").lower()).strip("-")[:30] + "-" + uuid.uuid4().hex[:6])
    run_dir = run_dir_for(run_id)
    os.makedirs(os.path.join(run_dir, "steps"), exist_ok=True)
    args = parse_args_value(ns.args)
    routes = load_routes(ns.routes)
    dispatcher = find_dispatcher()
    preflight = not (ns.no_preflight or os.environ.get("HEADLESS_WORKFLOW_NO_PREFLIGHT") == "1")
    write_run_meta(run_dir, run_id=run_id, name=meta.get("name"), description=meta.get("description"), script=os.path.abspath(ns.script),
                   args=args, status="running", started_at=now_iso(), resumed=bool(ns.resume), dispatcher=dispatcher, cwd=os.getcwd())
    print(f"RUN_ID : {run_id}\nRUN_DIR: {run_dir}\nLOG    : {os.path.join(run_dir, 'log.txt')}", flush=True)
    wf = Workflow(run_id, run_dir, routes, dispatcher, ns.max_agents, ns.concurrency, preflight, ns.quiet, args)
    wf.journal.write({"type": "run-started", "resumed": bool(ns.resume), "script": os.path.abspath(ns.script)})
    status = "completed"
    result = None
    error = None
    try:
        result = asyncio.run(mod.main(wf, args))
        with open(os.path.join(run_dir, "result.json"), "w") as fh:
            json.dump(result, fh, indent=1, ensure_ascii=False, default=lambda o: o.to_dict() if hasattr(o, "to_dict") else str(o))
    except WorkflowError as e:
        status, error = "failed", str(e)
    except Exception as e:  # noqa: BLE001
        status, error = "failed", f"{type(e).__name__}: {e}"
    wf.journal.write({"type": "run-finished", "status": status, "error": error, "dispatched": wf.dispatched})
    write_run_meta(run_dir, status=status, error=error, finished_at=now_iso(), dispatched=wf.dispatched)
    if status != "completed":
        print(f"headless-workflow: run {run_id} {status}: {error}", file=sys.stderr)
        return 1
    print(f"RESULT : {os.path.join(run_dir, 'result.json')}\nSTATUS : completed ({wf.dispatched} dispatches)", flush=True)
    return 0


def cmd_status(ns):
    run_dir = run_dir_for(ns.run_id)
    if not os.path.isdir(run_dir):
        raise SystemExit(f"no such run: {ns.run_id}")
    meta = {}
    if os.path.exists(os.path.join(run_dir, "run.json")):
        with open(os.path.join(run_dir, "run.json")) as fh:
            meta = json.load(fh)
    j = Journal(run_dir)
    print(f"run {ns.run_id}: {meta.get('status', '?')} (started {meta.get('started_at')}, finished {meta.get('finished_at')}, dispatched {meta.get('dispatched', '?')})")
    if meta.get("error"):
        print(f"error: {meta['error']}")
    phase = None
    with open(j.path) as fh:
        for lineno, line in enumerate(fh, 1):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                print(f"  [{'corrupt':9}] journal line {lineno}: non-mapping record ignored")
                continue
            if e.get("type") == "phase":
                title = e.get("title")
                if not isinstance(title, str):
                    print(f"  [{'corrupt':9}] journal line {lineno}: phase record without title ignored")
                    continue
                phase = title
                print(f"== {phase}")
            elif e.get("type") in ("started", "completed", "failed"):
                state = e["type"]
                extra = ""
                if state == "completed":
                    r = e.get("result")
                    if not isinstance(r, dict):
                        print(f"  [{'incomplete':9}] {e.get('label')} (completed record without mapping result ignored)")
                        continue
                    extra = f" route={r.get('route')} session={r.get('session_id')} attempts={r.get('attempts')}"
                elif state == "failed":
                    extra = f" error={e.get('error')}"
                print(f"  [{state:9}] {e.get('label')}{extra}")
    return 0


def cmd_result(ns):
    p = os.path.join(run_dir_for(ns.run_id), "result.json")
    if not os.path.exists(p):
        raise SystemExit(f"no result for run {ns.run_id} (still running or failed)")
    with open(p) as fh:
        sys.stdout.write(fh.read())
    return 0


def cmd_list(ns):
    root = os.path.join(STATE_ROOT, "runs")
    if not os.path.isdir(root):
        return 0
    for rid in sorted(os.listdir(root)):
        p = os.path.join(root, rid, "run.json")
        status = "?"
        if os.path.exists(p):
            with open(p) as fh:
                status = json.load(fh).get("status", "?")
        print(f"{rid}\t{status}")
    return 0


def cmd_routes(ns):
    routes = load_routes(ns.routes)
    for name, spec in routes.items():
        print(f"{name:9} {spec['harness']}/{spec['provider']}/{spec['model']} effort={spec.get('effort')} posture={spec.get('posture')} max={spec.get('max_concurrency')} fallback={spec.get('fallback')} fork={'yes' if route_supports_fork(spec) else 'no'} account={account_scope(spec) or '-'}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="headless-workflow", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a workflow script")
    r.add_argument("script")
    r.add_argument("--args", help="JSON value or @file passed to main(wf, args)")
    r.add_argument("--resume", metavar="RUN_ID", help="reuse a previous run's journal; unchanged steps return cached")
    r.add_argument("--routes", help="routes.json overlay")
    r.add_argument("--max-agents", type=int, default=200)
    r.add_argument("--concurrency", type=int, default=8, help="global concurrent dispatch cap")
    r.add_argument("--no-preflight", action="store_true", help="skip check-ai-quota preflight")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("status", help="show a run's steps")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_status)
    g = sub.add_parser("result", help="print a run's result JSON")
    g.add_argument("run_id")
    g.set_defaults(fn=cmd_result)
    l = sub.add_parser("list", help="list runs")
    l.set_defaults(fn=cmd_list)
    t = sub.add_parser("routes", help="print the effective route table")
    t.add_argument("--routes")
    t.set_defaults(fn=cmd_routes)
    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
