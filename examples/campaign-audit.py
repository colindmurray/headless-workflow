"""Audit a campaign corpus: cheap digest, strong judge per lane, consequence
check on P0s, cheap evidence check, strong synthesis. The strong route pays
only for judgment.

python3 scripts/headless-workflow.py run examples/campaign-audit.py \
  --args @audit-args.json --concurrency 8

From Codex, start that as a background cell and make ONE
write_stdin(chars:"", yield_time_ms: <expected minutes x 60000>).

The report is a candidate for the operator's judgment, not a decision.
"""
META = {"name": "campaign-audit", "description": "digest, judge per lane, consequence check, evidence check, synthesize"}
DIGEST = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array"}}}
FINDINGS = {"type": "object", "required": ["findings", "summary"], "properties": {"findings": {"type": "array"}}}
VERDICT = {"type": "object", "required": ["refuted", "confidence", "reasoning"],
           "properties": {"refuted": {"type": "boolean"}}}

CHECK_FIELDS = [("Id", "id"), ("Area", "area"), ("Severity", "severity"), ("Title", "title"),
                ("Claim", "claim"), ("Evidence", "evidence"), ("Recommendation", "recommendation"),
                ("Action", "action_type"), ("Confidence", "confidence")]
REPORT_FIELDS = [("Id", "id"), ("Area", "area"), ("Severity", "severity"), ("Title", "title"),
                 ("Evidence", "evidence"), ("Recommendation", "recommendation"),
                 ("Action", "action_type"), ("Confidence", "confidence")]


def render(finding, fields, extra=None):
    """A finding as plain `Key: value` lines, so the prompt stays readable."""
    lines = ["{}: {}".format(name, finding.get(key, "")) for name, key in fields]
    for name, value in (extra or []):
        if value:
            lines.append("{}: {}".format(name, value))
    return "\n".join(lines)


def render_corpus(items):
    return "\n".join(
        "- {}: {}\n  claims: {}\n  flags: {}".format(
            it.get("file", "?"), it.get("summary", ""),
            "; ".join(str(c) for c in (it.get("claims") or [])),
            "; ".join(str(f) for f in (it.get("flags") or [])))
        for it in items)


async def main(wf, args):
    root = args["root"]
    files = list(args.get("files") or [])
    batch = args.get("batch", 16)
    size = int(16 if batch is None else batch)
    if size < 1:
        raise ValueError("batch must be >= 1")
    lanes = list(args.get("lanes") or [])
    for ln in lanes:
        if not isinstance(ln, dict) or not ln.get("key") or not ln.get("prompt"):
            raise ValueError("each lane needs a key and a prompt")
    cap = int(args.get("p0_check_cap", 8))

    wf.phase("digest")
    batches = [files[i:i + size] for i in range(0, len(files), size)]
    read = await wf.parallel([
        (lambda b=b, i=i: wf.agent(
            "Read every file listed below, relative to " + root + ", and return JSON\n"
            '{"items": [{"file": "...", "summary": "...", "claims": ["..."], "flags": ["..."]}]}\n'
            "with one item per file: summary is at most two sentences, claims are literal statements "
            "the file makes, flags are contradictions, stale dates, or unowned work.\n\nFiles:\n"
            + "\n".join("- " + f for f in b),
            route=args.get("digest_route", "gemini"), schema=DIGEST, dir=root, label="digest:{}".format(i)))
        for i, b in enumerate(batches)])
    returned = [item for r in read if r for item in (r.get("items") or [])]
    digests = [item for item in returned if isinstance(item, dict)]
    if len(digests) != len(returned):
        wf.log("digest: dropped {} non-object item(s)".format(len(returned) - len(digests)))
    lost = sum(1 for r in read if not r)
    if lost:
        wf.log("digest: {} of {} batches returned nothing".format(lost, len(batches)))
    wf.log("digest: {} items from {} batches".format(len(digests), len(batches)))
    if files and not digests:
        wf.log("digest: no items from any batch; refusing to judge an empty corpus")
        return {"digests": [], "lanes": [], "report": None,
                "counts": {"findings": 0, "p0": 0, "refuted_by_check": 0,
                           "refuted_by_verify": 0, "kept": 0}}
    corpus = render_corpus(digests)

    wf.phase("judge")
    judged = await wf.parallel([
        (lambda ln=ln: wf.agent(
            "You audit the campaign corpus under {}. Your lane is '{}'.\n{}\n\n"
            "Digests of the whole corpus:\n{}\n\n"
            'Return JSON {{"findings": [{{"id", "area", "severity", "title", "claim", "evidence", '
            '"recommendation", "action_type", "confidence"}}], "summary": "...", "not_read": ["..."]}}. '
            "severity is P0, P1, P2 or P3; id is unique within the lane; evidence cites a file and a line "
            "or heading; action_type is apply-now, propose, or operator-decision; confidence is 0-1. List "
            "in not_read anything the lane needed that no digest covers.".format(
                root, ln["key"], ln["prompt"], corpus),
            route=args.get("judge_route", "opus"), schema=FINDINGS, label="judge:{}".format(ln["key"])))
        for ln in lanes])

    rows = []
    for ln, res in zip(lanes, judged):
        if not res:
            wf.log("judge: lane {} returned no findings".format(ln["key"]))
            continue
        raw = res.get("findings") or []
        found = sorted([f for f in raw if isinstance(f, dict)], key=lambda f: str(f.get("id", "")))
        if len(found) != len(raw):
            wf.log("judge: lane {}: dropped {} non-object finding(s)".format(
                ln["key"], len(raw) - len(found)))
        p0s = [i for i, f in enumerate(found) if str(f.get("severity", "")).upper() == "P0"]
        checkable = set(p0s[:cap])
        dropped = len(p0s) - len(checkable)
        if dropped:
            wf.log("check: lane {}: {} P0 finding(s) beyond p0_check_cap={} left unchecked".format(
                ln["key"], dropped, cap))
        for i, f in enumerate(found):
            rows.append({"lane": ln["key"], "finding": f, "do_check": i in checkable})

    wf.phase("consequence and evidence check")

    async def consequence(row, index):
        if not row["do_check"]:
            return dict(row, check=None)
        r = await wf.agent(
            "A campaign audit produced the P0 finding below. Try to REFUTE it: acting on it would be "
            "wrong if it duplicates work the campaign under {} has already done, fights a decision the "
            "corpus records, or costs more than the problem. Return JSON "
            '{{"refuted": bool, "confidence": 0-1, "reasoning": "...", "corrected_recommendation": "...", '
            '"severity_override": "P0|P1|P2|P3"}} — the last two only when the finding survives in a '
            "weakened form.\n\n{}".format(root, render(row["finding"], CHECK_FIELDS)),
            route=args.get("check_route", "opus"), schema=VERDICT, dir=root,
            label="check:{}:{}".format(row["lane"], row["finding"].get("id")))
        return dict(row, check=(r.data if r else None))

    async def evidence(prev, row, index):
        verdict = prev.get("check")
        if verdict and verdict.get("refuted"):
            return dict(prev, verify=None, kept=False)
        r = await wf.agent(
            "Re-read the evidence this finding cites under {} and decide whether it says what the finding "
            "claims. Default refuted=true when the citation is missing, points elsewhere, or you are "
            'unsure. Return JSON {{"refuted": bool, "confidence": 0-1, "reasoning": "..."}}.\n\n{}'.format(
                root, render(prev["finding"], CHECK_FIELDS)),
            route=args.get("verify_route", "glm"), schema=VERDICT, dir=root,
            label="verify:{}:{}".format(prev["lane"], prev["finding"].get("id")))
        v = r.data if r else {"refuted": True, "confidence": 0.0, "reasoning": "evidence check returned no verdict"}
        return dict(prev, verify=v, kept=not v.get("refuted", True))

    checked = [c for c in await wf.pipeline(rows, consequence, evidence) if c]
    kept = [c for c in checked if c["kept"]]
    counts = {
        "findings": len(checked),
        "p0": sum(1 for c in checked if str(c["finding"].get("severity", "")).upper() == "P0"),
        "refuted_by_check": sum(1 for c in checked if c.get("check") and c["check"].get("refuted")),
        "refuted_by_verify": sum(1 for c in checked if c.get("verify") and c["verify"].get("refuted")),
        "kept": len(kept),
    }
    wf.log("checked {findings} findings ({p0} P0): {refuted_by_check} refuted by consequence, "
           "{refuted_by_verify} by evidence, {kept} kept".format(**counts))

    wf.phase("synthesize")
    report = None
    if kept:
        blocks = "\n\n".join(
            render(c["finding"], REPORT_FIELDS, extra=[
                ("Lane", c["lane"]),
                ("Corrected recommendation", (c.get("check") or {}).get("corrected_recommendation")),
                ("Severity override", (c.get("check") or {}).get("severity_override"))])
            for c in kept)
        written = await wf.agent(
            "Write the campaign audit report in Markdown from the findings below, each of which has "
            "already survived a consequence check and an evidence check. Group them under "
            "'## Apply now', '## Propose', and '## Operator decision' following each Action, order by "
            "severity inside each group, and open with a summary of at most three sentences. Say what to "
            "change and why it is safe; invent nothing.\n\n" + blocks,
            route=args.get("synth_route", "opus"), label="synth")
        report = written.text if written else None
    else:
        wf.log("synthesize: nothing survived both checks; no report written")

    out_lanes = []
    for ln, res in zip(lanes, judged):
        mine = [c for c in checked if c["lane"] == ln["key"]]
        out_lanes.append({
            "key": ln["key"],
            "summary": res.get("summary") if res else None,
            "not_read": (res.get("not_read") or []) if res else [],
            "findings": [dict(c["finding"], check=c.get("check"), verify=c.get("verify"), kept=c["kept"])
                         for c in mine],
        })
    return {"digests": digests, "lanes": out_lanes, "report": report, "counts": counts}
