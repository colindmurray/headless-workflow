"""Cell-granular resume: semantic success predicate plus evidence-digest identity.

All tests run against tests/fake-headless-agent.sh so no provider is touched.
The fake dispatcher answers from markers embedded in the prompt
(`@@JSON:{...}@@`), which is how the probe script steers verdicts.

Regression tests pin the two guarantees; mutation tests run the same
scenarios against a neutered copy of the orchestrator and assert the mutant
behaves differently — so each mutation test goes red when its guard is
neutralized in the real source.
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

from test_headless_workflow import Harness, load_module

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "headless-workflow.py"
PROBE = SKILL / "examples" / "cell-resume-probe.py"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


PASS = '@@JSON:{"verdict": "pass"}@@'
FAIL = '@@JSON:{"verdict": "fail"}@@'


def probe_args(cells, route="glm"):
    return json.dumps({"cells": cells, "route": route})


def journal_events(h, run_id):
    journal = pathlib.Path(h.state) / "runs" / run_id / "journal.jsonl"
    return [json.loads(l) for l in journal.read_text().splitlines()]


def logged_calls(h):
    with open(os.path.join(h.fake_root, "calls.log")) as fh:
        return [json.loads(l) for l in fh if l.strip()]


class TestCellResume(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def cells(self, a_marker, b_marker, a_digest="dig-a1", b_digest="dig-b1"):
        return [
            {"id": "A", "evidence": "cite-a " + a_marker, "evidence_digest": a_digest, "neighbors": ["B"]},
            {"id": "B", "evidence": "cite-b " + b_marker, "evidence_digest": b_digest, "neighbors": ["A"]},
        ]

    def test_semantic_fail_is_not_cached_and_resume_retries_only_failed_cells(self):
        run_id = self.h.run_id_from(self.h.run(
            "run", str(PROBE), "--args", probe_args(self.cells(PASS, FAIL))))
        res = self.h.result(run_id)
        by_id = {c["id"]: c for c in res}
        self.assertTrue(by_id["A"]["ok"])
        self.assertEqual(by_id["A"]["verdict"], "pass")
        self.assertFalse(by_id["B"]["ok"], "a transport-ok verdict of fail must not report ok")
        self.assertEqual(by_id["B"]["verdict"], "fail")
        self.assertIn("semantic", (by_id["B"]["error"] or "").lower())
        self.assertEqual(len(self.h.calls()), 2)

        kinds = {}
        for e in journal_events(self.h, run_id):
            if e.get("type") in ("completed", "failed") and e.get("label"):
                kinds[e["label"]] = e["type"]
        self.assertEqual(kinds.get("review:A"), "completed")
        self.assertEqual(kinds.get("review:B"), "failed",
                         "a semantic fail must journal as failed, never completed")

        # unchanged resume: exactly one new dispatch, for the failed cell only
        self.h.run("run", str(PROBE), "--args", probe_args(self.cells(PASS, FAIL)),
                   "--resume", run_id)
        calls = self.h.calls()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2]["label"], "review:B")
        res2 = self.h.result(run_id)
        self.assertEqual({c["id"]: c["verdict"] for c in res2}, {"A": "pass", "B": "fail"})

    def test_repaired_evidence_with_new_digest_passes_without_redispatching_passing_cells(self):
        run_id = self.h.run_id_from(self.h.run(
            "run", str(PROBE), "--args", probe_args(self.cells(PASS, FAIL))))
        self.assertEqual(len(self.h.calls()), 2)
        repaired = self.cells(PASS, "cite-b-fixed " + PASS, b_digest="dig-b2")
        self.h.run("run", str(PROBE), "--args", probe_args(repaired), "--resume", run_id)
        calls = self.h.calls()
        self.assertEqual(len(calls), 3, "only the repaired cell may redispatch")
        self.assertEqual(calls[2]["label"], "review:B")
        by_id = {c["id"]: c for c in self.h.result(run_id)}
        self.assertTrue(by_id["A"]["ok"])
        self.assertTrue(by_id["B"]["ok"])
        self.assertEqual(by_id["B"]["verdict"], "pass")

    def test_evidence_digest_change_alone_changes_retry_identity(self):
        script = self.h.write_script("""
        META = {"name": "digest-id", "description": "digest identity"}
        async def main(wf, args):
            a = await wf.agent("fixed prompt @@REPLY:done@@", route="glm", label="one",
                               evidence_digest=args["d1"])
            b = await wf.agent("fixed prompt @@REPLY:done@@", route="glm", label="two",
                               evidence_digest=args["d2"])
            return [a.text, b.text]
        """)
        proc = self.h.run("run", script, "--args", json.dumps({"d1": "dd-1", "d2": "dd-2"}))
        run_id = self.h.run_id_from(proc)
        self.assertEqual(len(self.h.calls()), 2,
                         "identical prompts with different digests must not share a cache entry")
        # unchanged resume: fully cached
        self.h.run("run", script, "--args", json.dumps({"d1": "dd-1", "d2": "dd-2"}),
                   "--resume", run_id)
        self.assertEqual(len(self.h.calls()), 2)
        # one digest repaired: exactly one new dispatch
        self.h.run("run", script, "--args", json.dumps({"d1": "dd-1", "d2": "dd-3"}),
                   "--resume", run_id)
        calls = self.h.calls()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2]["label"], "two")

    def test_neighbors_are_part_of_the_step_identity(self):
        m = load_module()
        base = dict(kind="agent", prompt="p", route_name="glm", overrides={}, schema=None, parent=None)
        self.assertEqual(m.step_key(**base), m.step_key(**base))
        self.assertEqual(m.step_key(**base, neighbors=["A", "B"]),
                         m.step_key(**base, neighbors=["B", "A"]),
                         "neighbor order must not matter")
        self.assertNotEqual(m.step_key(**base, neighbors=["A"]),
                            m.step_key(**base, neighbors=["B"]))
        self.assertNotEqual(m.step_key(**base, cell="A"), m.step_key(**base, cell="B"))
        self.assertNotEqual(m.step_key(**base, evidence_digest="d1"),
                            m.step_key(**base, evidence_digest="d2"))
        self.assertNotEqual(m.step_key(**base, input_identity="i1"),
                            m.step_key(**base, input_identity="i2"))
        self.assertEqual(m.step_key(**base), m.step_key(**base, cell=None, neighbors=None,
                                                         evidence_digest=None, input_identity=None))

    def test_adopting_a_success_predicate_retries_stale_cached_failures(self):
        plain = self.h.write_script("""
        META = {"name": "plain", "description": "no predicate"}
        SCHEMA = {"type": "object", "required": ["verdict"]}
        async def main(wf, args):
            r = await wf.agent("review @@JSON:{\\"verdict\\": \\"fail\\"}@@",
                               route="glm", schema=SCHEMA, label="review")
            return {"ok": r.ok}
        """)
        run_id = self.h.run_id_from(self.h.run("run", plain))
        self.assertTrue(self.h.result(run_id)["ok"])
        strict = self.h.write_script("""
        META = {"name": "plain", "description": "no predicate"}
        SCHEMA = {"type": "object", "required": ["verdict"]}
        def is_pass(r):
            return (r.data or {}).get("verdict") == "pass"
        async def main(wf, args):
            r = await wf.agent("review @@JSON:{\\"verdict\\": \\"fail\\"}@@",
                               route="glm", schema=SCHEMA, label="review", success=is_pass)
            return {"ok": r.ok}
        """, name="wf2.py")
        self.h.run("run", strict, "--resume", run_id)
        self.assertEqual(len(self.h.calls()), 2,
                         "a cached transport-ok fail must be redispatched once a predicate rejects it")
        self.assertFalse(self.h.result(run_id)["ok"])

    def test_ordinary_agent_without_success_keeps_transport_ok_caching(self):
        script = self.h.write_script("""
        META = {"name": "plain", "description": "no predicate"}
        SCHEMA = {"type": "object", "required": ["verdict"]}
        async def main(wf, args):
            r = await wf.agent("review @@JSON:{\\"verdict\\": \\"fail\\"}@@",
                               route="glm", schema=SCHEMA, label="review")
            return {"ok": r.ok, "data": r.data}
        """)
        run_id = self.h.run_id_from(self.h.run("run", script))
        self.assertTrue(self.h.result(run_id)["ok"])
        self.h.run("run", script, "--resume", run_id)
        self.assertEqual(len(self.h.calls()), 1,
                         "the predicate is opt-in: plain agents behave exactly as before")

    def test_raising_success_predicate_is_a_retryable_failure(self):
        script = self.h.write_script("""
        META = {"name": "raises", "description": "raising predicate"}
        async def main(wf, args):
            r = await wf.agent("x @@REPLY:y@@", route="glm", label="r",
                               success=lambda r: r["missing"]["deep"])
            return {"ok": r.ok}
        """)
        run_id = self.h.run_id_from(self.h.run("run", script))
        self.assertFalse(self.h.result(run_id)["ok"])
        self.h.run("run", script, "--resume", run_id)
        self.assertEqual(len(self.h.calls()), 2,
                         "a predicate nobody can vouch for must never cache a pass")


class TestCellMutations(unittest.TestCase):
    """Each test runs its scenario against the real orchestrator and against a
    neutered copy, and passes only when the two behave differently in the
    expected direction. Neutralizing the guard or the digest input in the real
    source collapses that difference, so the test goes red."""

    def setUp(self):
        self.h = Harness()

    def run_script_path(self, script_path, *cli):
        proc = subprocess.run([sys.executable, str(script_path), *cli],
                              env=self.h.env(), capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                f"exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return proc

    def mutant_path(self, old, new):
        src = pathlib.Path(SCRIPT).read_text()
        self.assertIn(old, src, "mutant anchor drifted; update the mutation test")
        path = os.path.join(self.h.tmp, "mutant-hw.py")
        with open(path, "w") as fh:
            fh.write(src.replace(old, new, 1))
        return path

    def run_id_from(self, proc):
        for line in proc.stdout.splitlines():
            if line.startswith("RUN_ID"):
                return line.split(":", 1)[1].strip()
        raise AssertionError("no RUN_ID in output:\n" + proc.stdout)

    def test_mutation_semantic_guard_neutralized_caches_the_fail(self):
        mutant = self.mutant_path(
            "return bool(success(result))",
            "return True  # MUTANT: success predicate always passes")
        args = probe_args([
            {"id": "A", "evidence": "cite-a " + PASS, "evidence_digest": "m-a", "neighbors": []},
            {"id": "B", "evidence": "cite-b " + FAIL, "evidence_digest": "m-b", "neighbors": []},
        ])
        real_id = self.run_id_from(self.run_script_path(SCRIPT, "run", str(PROBE), "--args", args))
        self.run_script_path(SCRIPT, "run", str(PROBE), "--args", args, "--resume", real_id)
        self.assertEqual(len(self.h.calls()), 3,
                         "real code must redispatch the semantic fail on resume")

        calls_log = os.path.join(self.h.fake_root, "calls.log")
        os.remove(calls_log)
        mut_id = self.run_id_from(self.run_script_path(mutant, "run", str(PROBE), "--args", args))
        self.run_script_path(mutant, "run", str(PROBE), "--args", args, "--resume", mut_id)
        mut_calls = len(logged_calls(self.h))
        self.assertEqual(mut_calls, 2, "neutralized guard caches the fail (the stale behavior)")

    def test_mutation_digest_ignored_shares_the_cache_entry(self):
        mutant = self.mutant_path(
            '        payload["evidence_digest"] = evidence_digest',
            '        pass  # MUTANT: evidence digest dropped from identity')
        script = self.h.write_script("""
        META = {"name": "digest-id", "description": "digest identity"}
        async def main(wf, args):
            a = await wf.agent("fixed prompt @@REPLY:done@@", route="glm", label="one",
                               evidence_digest=args["d1"])
            b = await wf.agent("fixed prompt @@REPLY:done@@", route="glm", label="two",
                               evidence_digest=args["d2"])
            return True
        """)
        # The journal cache loads at run start, so the digest only shows across
        # resumes: run, resume unchanged (cached), resume with one digest changed.
        rid = self.run_id_from(self.run_script_path(
            SCRIPT, "run", script, "--args", json.dumps({"d1": "mm-1", "d2": "mm-2"})))
        self.run_script_path(SCRIPT, "run", script,
                             "--args", json.dumps({"d1": "mm-1", "d2": "mm-2"}), "--resume", rid)
        self.assertEqual(len(self.h.calls()), 2)
        self.run_script_path(SCRIPT, "run", script,
                             "--args", json.dumps({"d1": "mm-1", "d2": "mm-3"}), "--resume", rid)
        self.assertEqual(len(self.h.calls()), 3, "real code retires the entry on digest change")

        calls_log = os.path.join(self.h.fake_root, "calls.log")
        os.remove(calls_log)
        mid = self.run_id_from(self.run_script_path(
            mutant, "run", script, "--args", json.dumps({"d1": "mm-1", "d2": "mm-2"})))
        self.run_script_path(mutant, "run", script,
                             "--args", json.dumps({"d1": "mm-1", "d2": "mm-2"}), "--resume", mid)
        self.run_script_path(mutant, "run", script,
                             "--args", json.dumps({"d1": "mm-1", "d2": "mm-3"}), "--resume", mid)
        self.assertEqual(len(logged_calls(self.h)), 2,
                         "dropped digest serves the stale entry (the stale behavior)")


if __name__ == "__main__":
    unittest.main()
