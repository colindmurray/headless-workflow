"""Shared-context fork fan-out: one worker reads, forked children answer.

python3 scripts/headless-workflow.py run examples/fork-fanout.py \
  --args '{"spec": "README.md", "questions": ["What does it install?", "How is it tested?"]}'
"""
META = {"name": "fork-fanout", "description": "read once, answer many via native fork"}
ANSWER = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}

async def main(wf, args):
    parent = await wf.agent(f"Read {args['spec']} carefully. Reply with exactly: READY", route="glm", label="context")
    kids = await wf.parallel([
        (lambda q=q: wf.fork(parent, f"Answer from what you read, as JSON {{answer}}: {q}", schema=ANSWER, label=q[:40]))
        for q in args["questions"]])
    return [{"question": q, "answer": (k.data or {}).get("answer") if k else None, "forked": bool(k and k.forked)}
            for q, k in zip(args["questions"], kids)]
