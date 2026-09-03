# headless-workflow

Portable skill: one Python process runs a workflow script over many
`headless-agent` workers with a durable journal, `--resume`, native `fork`,
route fallback, structured-output repair, and per-route concurrency. It gives
a Codex or Claude Code supervisor the shape of Claude Code's Workflow tool
(`agent`, `parallel`, `pipeline`) across every headless provider, so a swarm
costs the caller one long wait instead of a turn per worker.

- `SKILL.md` — when to use, script shape, run/wait/resume, routes, fork
- `scripts/headless-workflow.py` — the orchestrator (stdlib only, Python 3.9+)
- `references/api.md` — full API, route fields, run-directory layout, CLI
- `references/patterns.md` — digest→judge→verify→synthesize, shared-context
  fork fan-out, loop-until-dry, issue swarm, cheap Codex wait
- `examples/` — runnable scripts
- `tests/` — unit tests driven by `tests/fake-headless-agent.sh`, a stand-in
  that mirrors the real dispatcher's stdout and run-dir contract

## Requires

`headless-agent` (dispatcher) and, for quota preflight, `check-ai-quota`,
both found automatically under the skillshare, Claude, or Codex skill roots.

## Install

Symlink or copy `headless-workflow` into any harness-defined skill root, or
`skillshare install /path/to/custom-skills/headless-workflow`.

## Test

```bash
python3 -m unittest discover -s tests -v
```
