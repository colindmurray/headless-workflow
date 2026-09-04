"""Tests for the runnable scripts under examples/.

Each example is driven end to end through tests/fake-headless-agent.sh, so no
provider is touched. Behaviour is steered entirely from --args: the fixture
values below carry the fake dispatcher's markers, which decide what each stage
answers.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "headless-workflow.py"
FAKE = HERE / "fake-headless-agent.sh"
CAMPAIGN_AUDIT = SKILL / "examples" / "campaign-audit.py"


def marker(obj):
    """A `@@JSON:...@@` marker the fake dispatcher answers with."""
    return "@@JSON:" + json.dumps(obj, separators=(",", ":")) + "@@"


def nested_marker(obj):
    """A marker whose payload carries markers of its own: every '@' is written
    as its JSON escape, so the outer marker holds no '@' and the dispatcher
    matches it whole. Parsing the reply restores the inner markers."""
    return "@@JSON:" + json.dumps(obj, separators=(",", ":")).replace("@", "\\u0040") + "@@"


DIGEST_ITEMS = [
    {"file": "goals/goal-01.md", "summary": "the frontier goal", "claims": ["the tree lane is audited weekly"], "flags": []},
    {"file": "issues/issue-07.md", "summary": "an open issue", "claims": ["issue-07 blocks goal-01"], "flags": ["unowned"]},
]
CORPUS_FILE = "corpus/campaign.md " + marker({"items": DIGEST_ITEMS})

CHECK_HOLDS = marker({"refuted": False, "confidence": 0.9, "reasoning": "holds"})
CHECK_FAILS = marker({"refuted": True, "confidence": 0.8, "reasoning": "nope"})

KEPT_TITLE = "Frontier goal records no owner"
DROPPED_TITLE = "Lane prompt duplicates a skill"
LANE_SUMMARY = "two findings in the goal tree lane"

FINDING_P0 = {"id": "F1", "area": "goal-tree", "severity": "P0", "title": KEPT_TITLE,
              "claim": "goal-01 names no owner " + CHECK_HOLDS,
              "evidence": "goals/goal-01.md, heading Owner",
              "recommendation": "Name an owner on goal-01", "action_type": "apply-now", "confidence": 0.9}
FINDING_P1 = {"id": "F2", "area": "lanes", "severity": "P1", "title": DROPPED_TITLE,
              "claim": "the lane prompt restates the skill " + CHECK_FAILS,
              "evidence": "issues/issue-07.md, line 12",
              "recommendation": "Point the lane at the skill", "action_type": "propose", "confidence": 0.6}


def lane_prompt(findings):
    return "Audit the goal tree for unowned or duplicated work. " + nested_marker(
        {"findings": findings, "summary": LANE_SUMMARY, "not_read": []})


LANE_PROMPT = lane_prompt([FINDING_P0, FINDING_P1])
# A judge that answers with a bare string where an object belongs.
MALFORMED_LANE_PROMPT = lane_prompt([FINDING_P0, "the frontier goal has no owner"])

STAGE_MODELS = [
    ("digest:", "gemini-3.8-flash"),
    ("judge:", "opus"),
    ("check:", "opus"),
    ("verify:", "glm-5.3-flash"),
    ("synth", "opus"),
]


class Harness:
    """One isolated state dir, corpus dir, and fake run root per test."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="hw-example-")
        self.state = os.path.join(self.tmp, "state")
        self.fake_root = os.path.join(self.tmp, "fake")
        self.corpus = os.path.join(self.tmp, "corpus")
        for d in (self.state, self.fake_root, self.corpus):
            os.makedirs(d)

    def env(self):
        env = dict(os.environ)
        env.update({
            "HEADLESS_WORKFLOW_DISPATCHER": str(FAKE),
            "HEADLESS_WORKFLOW_STATE": self.state,
            "HEADLESS_WORKFLOW_NO_PREFLIGHT": "1",
            "FAKE_RUN_ROOT": self.fake_root,
            # A path that does not exist, so route models come from the
            # built-in defaults rather than the operator's own routes.json.
            "HEADLESS_WORKFLOW_ROUTES": os.path.join(self.tmp, "no-routes.json"),
        })
        return env

    def run(self, *cli, **kw):
        check = kw.pop("check", True)
        timeout = kw.pop("timeout", 180)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *cli],
            env=self.env(), capture_output=True, text=True, timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return proc

    def calls(self):
        p = os.path.join(self.fake_root, "calls.log")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def run_id_from(self, proc):
        for line in proc.stdout.splitlines():
            if line.startswith("RUN_ID"):
                return line.split(":", 1)[1].strip()
        raise AssertionError("no RUN_ID line in output:\n" + proc.stdout)

    def run_dir(self, run_id):
        return pathlib.Path(self.state) / "runs" / run_id

    def result(self, run_id):
        return json.loads(self.run("result", run_id).stdout)

    def log_text(self, run_id):
        return (self.run_dir(run_id) / "log.txt").read_text()

    def prompt_for(self, run_id, label, attempt=1):
        """The exact prompt sent for the step carrying `label`."""
        journal = (self.run_dir(run_id) / "journal.jsonl").read_text().splitlines()
        for line in journal:
            e = json.loads(line)
            if e.get("type") == "completed" and e.get("label") == label:
                return (self.run_dir(run_id) / "steps" / e["key"] / f"prompt-{attempt}.txt").read_text()
        raise AssertionError(f"no completed step labelled {label!r}")

    def audit_args(self, **over):
        args = {
            "root": self.corpus,
            "files": [CORPUS_FILE],
            "batch": 16,
            "lanes": [{"key": "tree", "prompt": LANE_PROMPT}],
            "p0_check_cap": 8,
        }
        args.update(over)
        return json.dumps(args)


class TestCampaignAudit(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def audit(self, **over):
        args = self.h.audit_args(**over)
        proc = self.h.run("run", str(CAMPAIGN_AUDIT), "--args", args)
        return args, self.h.run_id_from(proc)

    def test_five_stages_keep_only_the_finding_that_survives_both_checks(self):
        args, run_id = self.audit()
        res = self.h.result(run_id)

        self.assertEqual(res["counts"], {
            "findings": 2, "p0": 1, "refuted_by_check": 0, "refuted_by_verify": 1, "kept": 1})
        self.assertEqual([d["file"] for d in res["digests"]],
                         ["goals/goal-01.md", "issues/issue-07.md"])

        lane = res["lanes"][0]
        self.assertEqual(lane["key"], "tree")
        self.assertEqual(lane["summary"], LANE_SUMMARY)
        f1, f2 = lane["findings"]
        self.assertEqual((f1["id"], f2["id"]), ("F1", "F2"))
        self.assertEqual(f1["check"], {"refuted": False, "confidence": 0.9, "reasoning": "holds"})
        self.assertEqual(f1["verify"], {"refuted": False, "confidence": 0.9, "reasoning": "holds"})
        self.assertTrue(f1["kept"])
        self.assertIsNone(f2["check"], "only P0 findings reach the consequence check")
        self.assertEqual(f2["verify"], {"refuted": True, "confidence": 0.8, "reasoning": "nope"})
        self.assertFalse(f2["kept"])

        self.assertEqual(res["report"], "OK: synth")

    def test_each_stage_runs_on_its_own_route(self):
        args, run_id = self.audit()
        calls = self.h.calls()
        self.assertEqual(sorted(c["label"] for c in calls),
                         ["check:tree:F1", "digest:0", "judge:tree", "synth", "verify:tree:F1", "verify:tree:F2"])
        for c in calls:
            expected = next(m for prefix, m in STAGE_MODELS if c["label"].startswith(prefix))
            self.assertEqual(c["model"], expected, c["label"])

    def test_synthesis_sees_only_the_surviving_findings(self):
        args, run_id = self.audit()
        prompt = self.h.prompt_for(run_id, "synth")
        self.assertIn(KEPT_TITLE, prompt)
        self.assertIn("Name an owner on goal-01", prompt)
        self.assertNotIn(DROPPED_TITLE, prompt)
        self.assertNotIn("@@", prompt, "the report prompt carries no dispatcher markers")

    def test_resume_redispatches_nothing(self):
        args, run_id = self.audit()
        before = len(self.h.calls())
        self.assertEqual(before, 6)
        self.h.run("run", str(CAMPAIGN_AUDIT), "--args", args, "--resume", run_id)
        self.assertEqual(len(self.h.calls()), before, "an unchanged rerun must serve every step from the journal")
        self.assertEqual(self.h.result(run_id)["counts"]["kept"], 1)

    def test_p0_check_cap_zero_skips_the_check_stage_and_logs_the_drop(self):
        args, run_id = self.audit(p0_check_cap=0)
        labels = [c["label"] for c in self.h.calls()]
        self.assertEqual([l for l in labels if l.startswith("check:")], [])
        self.assertEqual(sorted(labels), ["digest:0", "judge:tree", "synth", "verify:tree:F1", "verify:tree:F2"])

        log = self.h.log_text(run_id)
        self.assertIn("1 P0 finding(s) beyond p0_check_cap=0", log)

        res = self.h.result(run_id)
        self.assertEqual(res["counts"], {
            "findings": 2, "p0": 1, "refuted_by_check": 0, "refuted_by_verify": 1, "kept": 1})
        self.assertIsNone(res["lanes"][0]["findings"][0]["check"])

    def test_a_judge_answering_with_a_bare_string_loses_only_that_finding(self):
        args, run_id = self.audit(lanes=[{"key": "tree", "prompt": MALFORMED_LANE_PROMPT}])

        self.assertIn("judge: lane tree: dropped 1 non-object finding(s)", self.h.log_text(run_id))
        res = self.h.result(run_id)
        self.assertEqual(res["counts"], {
            "findings": 1, "p0": 1, "refuted_by_check": 0, "refuted_by_verify": 0, "kept": 1})
        self.assertEqual([f["id"] for f in res["lanes"][0]["findings"]], ["F1"])
        self.assertEqual(res["report"], "OK: synth")

    def test_an_empty_corpus_is_not_judged(self):
        empty = "corpus/empty.md " + marker({"items": []})
        args, run_id = self.audit(files=[empty])

        self.assertEqual([c["label"] for c in self.h.calls()], ["digest:0"],
                         "the strong routes never run against a corpus nothing was read from")
        self.assertIn("refusing to judge an empty corpus", self.h.log_text(run_id))
        res = self.h.result(run_id)
        self.assertEqual(res, {
            "digests": [], "lanes": [], "report": None,
            "counts": {"findings": 0, "p0": 0, "refuted_by_check": 0, "refuted_by_verify": 0, "kept": 0}})

    def test_a_batch_size_below_one_is_refused_before_any_dispatch(self):
        proc = self.h.run("run", str(CAMPAIGN_AUDIT), "--args", self.h.audit_args(batch=0), check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ValueError: batch must be >= 1", proc.stderr)
        self.assertEqual(self.h.calls(), [])

    def test_a_lane_missing_its_key_is_refused_before_any_dispatch(self):
        args = self.h.audit_args(lanes=[{"prompt": LANE_PROMPT}])
        proc = self.h.run("run", str(CAMPAIGN_AUDIT), "--args", args, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ValueError: each lane needs a key and a prompt", proc.stderr)
        self.assertEqual(self.h.calls(), [])


if __name__ == "__main__":
    unittest.main()
