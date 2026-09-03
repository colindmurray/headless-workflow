"""Digest with cheap workers, judge with a strong one, verify cheaply.

python3 scripts/headless-workflow.py run examples/digest-judge-verify.py \
  --args '{"root": "/path/to/docs", "files": ["a.md", "b.md", "c.md"], "judge_route": "opus"}'
"""
META = {"name": "digest-judge-verify", "description": "cheap digest, strong judge, cheap verify"}
DIGEST = {"type": "object", "required": ["summary", "claims"], "properties": {"claims": {"type": "array"}}}
FINDINGS = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array"}}}
VERDICT = {"type": "object", "required": ["refuted", "reasoning"], "properties": {"refuted": {"type": "boolean"}}}

async def main(wf, args):
    wf.phase("digest")
    digests = await wf.parallel([
        (lambda f=f: wf.agent(f"Read {f} under {args['root']} and return JSON {{summary, claims:[...]}}; claims are literal statements the file makes.",
                              route="gemini", schema=DIGEST, dir=args["root"], label=f"digest:{f}"))
        for f in args["files"]])
    wf.phase("judge")
    judge = await wf.agent("Digests:\n" + "\n".join(f"- {f}: {d.data}" for f, d in zip(args["files"], digests) if d)
                           + "\nReturn JSON {findings:[{title, claim, file}]} for contradictions or risks.",
                           route=args.get("judge_route", "opus"), schema=FINDINGS, label="judge")
    if not judge:
        return {"error": judge.error}
    wf.phase("verify")
    verdicts = await wf.pipeline(
        judge.data["findings"],
        lambda f, i: wf.agent(f"Check this finding against the actual file under {args['root']}; default refuted=true when unsure. Return JSON {{refuted, reasoning}}. Finding: {f}",
                              route="glm", schema=VERDICT, dir=args["root"], label=f"verify-{i}"))
    return [{"finding": f, "refuted": (v.data["refuted"] if v else None)} for f, v in zip(judge.data["findings"], verdicts)]
