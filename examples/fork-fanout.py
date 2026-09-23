"""Explore once, fork many: the general runner. One explorer gathers a shared
context into its session. Then one native fork per task starts from that
cached context, and an optional synthesis step combines their outputs. The
material is read once at full price; every fork reads it from the provider's
prompt cache. Use it for any task list that needs the same context:
questions about one codebase or corpus, debugging hypotheses against one
reproduction, per-module migration plans, design alternatives from one spec,
audit dimensions, doc sections. See references/fork-fanout.md;
examples/fork-review.py is a code-review instance with a verify stage.

python3 scripts/headless-workflow.py run examples/fork-fanout.py --args '{
  "root": "/path/to/repo",
  "context": "the payments module (src/payments/**) and its tests",
  "tasks": ["Where can a refund be issued twice? Cite lines.",
            {"key": "plan", "prompt": "Plan the migration of src/payments to the v2 client, file by file."}],
  "synthesize": "Combine these answers into one prioritized action list."}'

A task is a string or {"key", "prompt", "schema"}. Optional args: "route"
(name or route dict; default glm), "model" (explorer model override, e.g. a
long-context model; forks inherit it), "posture", "schema" (default JSON
schema for every task), "synthesize" (a prompt; omit to skip),
"synth_route" (synthesize with a fresh agent on that route instead of a fork
of the explorer).
"""
import json

META = {"name": "fork-fanout", "description": "gather shared context once, fork one specialist per task"}

EXPLORE = """You are the shared context for a team of specialists who will be forked from this session.
Gather this into the conversation by reading it with your tools; the material you read is what they inherit:
{context}
Do not start on any specialist's task, and do not summarize the material.
When done, reply with a map of what you gathered only: one line per source, `path-or-source - what it holds`,
at most 60 lines."""

TASK = """{prompt}
Work from what is already in this conversation. Open new material only when this task needs something the shared
context does not have.{contract}"""


def task_parts(t, i):
    if isinstance(t, dict):
        return t.get("key") or f"task-{i + 1}", t["prompt"], t.get("schema")
    return f"task-{i + 1}", t, None


async def main(wf, args):
    route = args.get("route", "glm")
    explore_opts = {k: args[k] for k in ("model", "posture") if args.get(k)}

    wf.phase("Explore")
    # No fallback: forks must run on the explorer's route, and the cheap routes'
    # fallbacks cannot fork. require_native below turns that into a clear failure.
    explorer = await wf.agent(EXPLORE.format(context=args["context"]), route=route, dir=args["root"],
                              fallback=args.get("explorer_fallback", []), label="explore", **explore_opts)
    if not explorer:
        return {"error": f"explorer failed: {explorer.error}"}

    async def run(t, i):
        key, prompt, schema = task_parts(t, i)
        schema = schema or args.get("schema")
        contract = f"\nReply with JSON only, matching this schema: {json.dumps(schema)}" if schema else ""
        # the fork inherits route, model, effort, dir and posture: same cached prefix
        r = await wf.fork(explorer, TASK.format(prompt=prompt, contract=contract), schema=schema,
                          require_native=True, label=f"task:{key}", phase="Fan out")
        return {"key": key, "ok": bool(r), "output": (r.data if schema else r.text) if r else None,
                "error": None if r else r.error}

    results = await wf.parallel([(lambda t=t, i=i: run(t, i)) for i, t in enumerate(args["tasks"])])

    synthesis = None
    if args.get("synthesize"):
        wf.phase("Synthesize")
        prompt = args["synthesize"] + "\n\nSpecialist outputs:\n" + json.dumps([r for r in results if r], indent=1)
        if args.get("synth_route"):
            s = await wf.agent(prompt, route=args["synth_route"], dir=args["root"], label="synthesize")
        else:
            # a fork of the explorer can check the outputs against the material it already holds
            s = await wf.fork(explorer, prompt, require_native=True, label="synthesize")
        synthesis = s.text if s else f"synthesis failed: {s.error}"
    return {"explorer_session": explorer.session_id, "map": explorer.text, "results": results, "synthesis": synthesis}
