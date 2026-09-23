"""Explore once, fork many, applied to code review: one worked instance of the
general runner in examples/fork-fanout.py, with a verify stage added. One agent
reads the change into its context, one native fork per review dimension works
from that cached context, one fork of the explorer per dimension verifies that
dimension's findings, and one agent writes the report. The change is read
once at full price. The reviewers and, by default, the verifiers start from
the explorer's cached prefix; the synthesizer (and any verify_route
verifiers) are fresh. See references/fork-fanout.md.

python3 scripts/headless-workflow.py run examples/fork-review.py --args '{
  "root": "/path/to/repo",
  "target": "the patch at change.patch (from git diff origin/main...HEAD > change.patch)",
  "dimensions": ["correctness", "security", "concurrency", "error handling", "test coverage"],
  "route": "glm"}'

A dimension may be a string or {"key": ..., "focus": ...}. Optional args:
"verify_route" (verify with fresh agents on another route/model instead of
forks of the explorer, for independence), "synth_route", "max_findings" (per
dimension, default 8; the most severe are verified, the rest are listed as
"overflow"). For a change larger than about half the explorer model's window,
pass a route dict with a long-context model or shard (see the reference).
"""
import json

META = {"name": "fork-review", "description": "explore a change once, fork one reviewer and one verifier per dimension"}
FINDINGS = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array", "items": {
    "type": "object", "required": ["id", "title", "file", "claim"]}}}}
VERDICTS = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "refuted", "reasoning"], "properties": {"refuted": {"type": "boolean"}}}}}}

EXPLORE = """You are the shared context for a team of reviewers who will be forked from this session.
Read {target} in full, plus the surrounding code each hunk depends on, so that the change and its context are in
this conversation. Use the file-reading tools; the file contents you read are what the reviewers inherit.
Do not review, judge, or summarize the code yet.
When done, reply with a file map only: one line per touched file, `path - role in the change`, at most 60 lines."""

REVIEW = """Review the change you just read along ONE dimension only: {dim}.{focus}
Work from what is already in this conversation; re-open a file only to check a specific line you have not read.
Report only defects in the change (not pre-existing code) with a concrete failure scenario, most severe first.
Reply with JSON only: {{"findings": [{{"id": "{key}-1", "title": "...", "file": "path", "line": 0, "severity": "P0|P1|P2|P3", "claim": "what breaks and how"}}]}}.
An empty list is a valid answer."""

VERIFY = """Other reviewers reported the findings below about the change you read. For EACH one, try to refute it
against the code: is the scenario reachable, and is it caused by this change? Default to refuted=true when you
cannot confirm it. Reply with JSON only: {{"verdicts": [{{"id": "...", "refuted": true, "reasoning": "..."}}]}}.

{findings}"""


def dim_parts(d):
    if isinstance(d, dict):
        return d["key"], (" Focus: " + d["focus"]) if d.get("focus") else ""
    return d, ""


async def main(wf, args):
    root = args["root"]
    cap = args.get("max_findings", 8)

    wf.phase("Explore")
    # No fallback by default: a fork must run on the explorer's harness, and the cheap
    # routes' fallbacks (muse, gemini) cannot fork. If the explorer landed there, every
    # reviewer would fail (require_native) rather than silently re-read at full price.
    explorer = await wf.agent(EXPLORE.format(target=args["target"]), route=args.get("route", "glm"), dir=root,
                              fallback=args.get("explorer_fallback", []), label="explore")
    if not explorer:
        return {"error": f"explorer failed: {explorer.error}"}

    async def review(d, i):
        key, focus = dim_parts(d)
        # the fork inherits route, model, effort, dir and posture: same cached prefix
        return await wf.fork(explorer, REVIEW.format(dim=key, key=key.replace(" ", "-"), focus=focus), schema=FINDINGS,
                             require_native=True, label=f"review:{key}", phase="Review")

    async def verify(rev, d, i):
        key, _ = dim_parts(d)
        if not rev:
            return {"dimension": key, "error": rev.error, "confirmed": [], "dropped": [], "overflow": []}
        found = sorted(rev.data["findings"], key=lambda f: str(f.get("severity", "P9")))
        overflow = [f["id"] for f in found[cap:]]
        if overflow:
            wf.log(f"review:{key}: verifying the {cap} most severe of {len(found)} findings; {len(overflow)} listed as overflow")
        found = found[:cap]
        if not found:
            return {"dimension": key, "confirmed": [], "dropped": [], "overflow": overflow}
        listing = "\n".join(f"- [{f['id']}] {f['file']}:{f.get('line', '?')} {f['title']}: {f['claim']}" for f in found)
        prompt = VERIFY.format(findings=listing)
        if args.get("verify_route"):
            # fresh agent on another model: more independent, pays to re-read the cited code
            v = await wf.agent(f"The change under review: {args['target']}.\n" + prompt, route=args["verify_route"],
                               dir=root, schema=VERDICTS, label=f"verify:{key}", phase="Verify")
        else:
            # fork of the explorer, not of the reviewer: the code is cached, the reviewer's reasoning is not inherited
            v = await wf.fork(explorer, prompt, schema=VERDICTS, require_native=True, label=f"verify:{key}", phase="Verify")
        refuted = {x["id"] for x in (v.data["verdicts"] if v else []) if x.get("refuted")}
        judged = {x["id"] for x in (v.data["verdicts"] if v else [])}
        return {"dimension": key,
                "confirmed": [f for f in found if f["id"] in judged and f["id"] not in refuted],
                "dropped": [f["id"] for f in found if f["id"] in refuted],
                "unverified": [f["id"] for f in found if f["id"] not in judged],
                "overflow": overflow}

    per_dim = [r for r in await wf.pipeline(args["dimensions"], review, verify) if r]

    wf.phase("Synthesize")
    confirmed = [dict(f, dimension=r["dimension"]) for r in per_dim for f in r["confirmed"]]
    report = await wf.agent("Write a code-review report for these verified findings, most severe first, merging duplicates "
                            "reported under different dimensions:\n" + json.dumps(confirmed, indent=1),
                            route=args.get("synth_route", args.get("route", "glm")), label="synthesize")
    return {"explorer_session": explorer.session_id, "dimensions": per_dim,
            "confirmed": confirmed, "report": report.text if report else None}
