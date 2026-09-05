"""Cell-granular resume probe: one reviewer agent per cell with an explicit
semantic success predicate and an evidence-content digest.

Each cell is judged on its own step identity (cell id + declared neighbors +
evidence digest). A transport-ok reply whose verdict is not "pass" is
journaled as failed, never completed, so `--resume` retries only the
failed or unrun cells; repaired evidence under a new digest retires the old
cache entry without redispatching the cells that already passed.

Run it (fake dispatcher in tests; real providers work the same way):

python3 scripts/headless-workflow.py run examples/cell-resume-probe.py \
  --args '{"cells": [{"id": "A", "evidence": "goals/goal-01.md names no owner",
                      "evidence_digest": "aaa", "neighbors": ["B"]}], "route": "glm"}'

`evidence_digest` should be a deterministic digest (sha256) of the evidence
content behind the cell, recomputed whenever that content is repaired. Cells
that only change their digest get a fresh step key even when the prompt
template is unchanged.
"""
import hashlib

META = {"name": "cell-resume-probe", "description": "per-cell reviewer with semantic resume"}
REVIEW = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}


def cell_pass(result):
    """Semantic success: the reviewer must return verdict pass."""
    return bool(result.data) and result.data.get("verdict") == "pass"


def digest_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def main(wf, args):
    cells = list(args["cells"])
    route = args.get("route", "glm")

    async def review(cell):
        evidence = cell["evidence"]
        return await wf.agent(
            "Review cell {} against its cited evidence and reply with JSON "
            "holding a verdict key of pass or fail. Evidence: {}".format(cell["id"], evidence),
            route=route, schema=REVIEW, label="review:{}".format(cell["id"]),
            cell=cell["id"], neighbors=cell.get("neighbors", []),
            evidence_digest=cell.get("evidence_digest") or digest_of(evidence),
            success=cell_pass)

    outs = await wf.parallel([(lambda c=c: review(c)) for c in cells])
    # NB: a failed AgentResult is falsy, so test for None, not truthiness,
    # when reporting per-cell verdicts.
    return [{"id": c["id"], "ok": bool(o),
             "verdict": (o.data or {}).get("verdict") if o is not None and o.data else None,
             "error": (o.error if o is not None else "no result")}
            for c, o in zip(cells, outs)]
