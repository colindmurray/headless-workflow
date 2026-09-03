# headless-workflow — patterns

Shapes that have paid off. Each is a complete `main()`; combine freely.

## Digest → judge → verify → synthesize

Cheap models read everything and return compact JSON; a strong model reasons
over the digests; cheap models re-check each claim against the sources; one
strong model writes the deliverable. Claude/OpenAI spend is limited to the
judgment steps.

```python
META = {"name": "corpus-audit", "description": "cheap digest, strong judge, cheap verify, strong synth"}
DIGEST = {"type": "object", "required": ["items"]}
FINDINGS = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array"}}}
VERDICT = {"type": "object", "required": ["refuted", "reasoning"]}

async def main(wf, args):
    wf.phase("digest")
    batches = [args["files"][i:i + 16] for i in range(0, len(args["files"]), 16)]
    digests = await wf.parallel([
        (lambda b=b, i=i: wf.agent(f"Read these files and return JSON {{items:[...]}} describing each: {b}",
                                   route="gemini", schema=DIGEST, label=f"digest-{i}", dir=args["root"]))
        for i, b in enumerate(batches)])
    wf.phase("judge")
    judge = await wf.agent(f"Digests: {[d.data for d in digests if d]}\nReturn findings as JSON.",
                           route="opus", schema=FINDINGS, label="judge")
    wf.phase("verify")
    checked = await wf.pipeline(
        judge.data["findings"],
        lambda f, i: wf.agent(f"Refute or confirm against the files under {args['root']}: {f}",
                              route="glm", schema=VERDICT, label=f"verify-{i}"),
    )
    kept = [f for f, v in zip(judge.data["findings"], checked) if v and not v.data["refuted"]]
    wf.phase("synthesize")
    report = await wf.agent(f"Write the report for these confirmed findings: {kept}", route="opus", label="report")
    return {"confirmed": kept, "report": report.text}
```

## Shared-context fork fan-out

One worker builds the expensive context once; every child is a native fork,
so children start with the parent's history and the provider's cached
prefix instead of re-reading. Use a fork-capable route (`glm`, `sonnet`,
`opus`, `luna`, or `opencode` models).

```python
async def main(wf, args):
    parent = await wf.agent(f"Read {args['spec']} and the modules it names. Reply DONE when you hold the full picture.",
                            route="glm", label="context")
    kids = await wf.parallel([
        (lambda q=q: wf.fork(parent, f"Using what you read, answer only this: {q}", schema=ANSWER, label=q[:30]))
        for q in args["questions"]])
    return [k.data for k in kids if k]
```

If the route cannot fork, `fork()` still works by prepending the parent's
prompt and answer; keep the parent's answer compact when that fallback is
likely.

## Loop until dry

Unknown-size discovery: keep spawning finders until two consecutive rounds add
nothing new. Dedupe against everything seen, not only against what survived.

```python
async def main(wf, args):
    seen, kept, dry, rnd = set(), [], 0, 0
    while dry < 2 and rnd < 6:
        rnd += 1
        found = await wf.parallel([
            (lambda lens=lens: wf.agent(f"Find defects in {args['target']} through the {lens} lens; JSON {{bugs:[{{id,desc}}]}}",
                                        route="glm", schema=BUGS, label=f"find-{lens}-{rnd}"))
            for lens in ["correctness", "security", "concurrency"]])
        fresh = [b for r in found if r for b in r.data["bugs"] if b["id"] not in seen]
        if not fresh:
            dry += 1
            continue
        dry = 0
        seen.update(b["id"] for b in fresh)
        votes = await wf.parallel([(lambda b=b: wf.agent(f"Refute: {b}", route="muse", schema=VERDICT)) for b in fresh])
        kept += [b for b, v in zip(fresh, votes) if v and not v.data["refuted"]]
        wf.log(f"round {rnd}: {len(fresh)} fresh, {len(kept)} kept")
    return kept
```

## Issue swarm for a supervisor

A CAO/GitHub supervisor hands the whole eligible frontier to one run: each
item gets its own worktree and an implement→review chain; the supervisor
waits once and harvests the result list.

```python
async def main(wf, args):
    async def implement(issue, i):
        return await wf.agent(f"Implement {issue['key']} per its body: {issue['body']}. Commit on the current branch and report the head SHA.",
                              route="glm", posture="code", dir=issue["worktree"], label=f"impl:{issue['key']}")
    async def review(impl, issue, i):
        if not impl:
            return None
        return await wf.agent(f"Independently review the branch in this worktree for {issue['key']}. Return JSON {{verdict, findings}}.",
                              route="muse", schema=REVIEW, dir=issue["worktree"], label=f"review:{issue['key']}")
    outs = await wf.pipeline(args["issues"], implement, review)
    return [{"key": iss["key"], "review": (o.data if o else None)} for iss, o in zip(args["issues"], outs)]
```

Give every writer its own `dir`; never two `posture="code"` agents in one
worktree. Keep tracker claims and merges with the supervisor.

## Verify with independent lenses

```python
votes = await wf.parallel([
    (lambda lens=lens: wf.agent(f"{lens} lens. Default refuted=true if unsure. Claim: {claim}", route="gemini", schema=VERDICT))
    for lens in ["evidence", "consequence", "alternative"]])
survives = sum(1 for v in votes if v and not v.data["refuted"]) * 2 > len([v for v in votes if v])
```

## Cheap wait from Codex

```
exec_command:  python3 "$HW" run wf.py --args @args.json > /tmp/hw.log 2>&1   (yield_time_ms 1000)
write_stdin:   session_id=<cell>, chars="", yield_time_ms=<minutes × 60000>   (one call; repeat once if still running)
then:          python3 "$HW" status <RUN_ID>; python3 "$HW" result <RUN_ID>
```
