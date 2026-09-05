"""Journal coherence: validate-before-mutate replay plus ordered eligibility.

A cached completion older than the cell's latest structured failure must
never satisfy a retry; malformed journal records must be ignored safely,
never crash recovery, and never overwrite established good entries.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "headless-workflow.py"
FAKE = HERE / "fake-headless-agent.sh"


def load_module(path=None):
    spec = importlib.util.spec_from_file_location(
        "headless_workflow_coherence", path or SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Harness:
    """One isolated state dir + fake run root per test (mirrors the main suite)."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="hw-coherence-")
        self.state = os.path.join(self.tmp, "state")
        self.fake_root = os.path.join(self.tmp, "fake")
        os.makedirs(self.state)
        os.makedirs(self.fake_root)

    def env(self):
        env = dict(os.environ)
        env.update({
            "HEADLESS_WORKFLOW_DISPATCHER": str(FAKE),
            "HEADLESS_WORKFLOW_STATE": self.state,
            "HEADLESS_WORKFLOW_NO_PREFLIGHT": "1",
            "FAKE_RUN_ROOT": self.fake_root,
        })
        return env

    def write_script(self, body, name="wf.py"):
        import sys
        import textwrap
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body))
        return path

    def run(self, *cli, timeout=120):
        import subprocess
        import sys
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *cli],
            env=self.env(), capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise AssertionError(
                f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def run_id_from(self, out):
        for line in out.splitlines():
            if line.startswith("RUN_ID"):
                return line.split(":", 1)[1].strip()
        raise AssertionError("no RUN_ID in output:\n" + out)

    def calls(self):
        log = os.path.join(self.fake_root, "calls.log")
        if not os.path.exists(log):
            return []
        with open(log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def result(self, run_id):
        with open(os.path.join(self.state, "runs", run_id, "result.json")) as fh:
            return json.load(fh)


class TestJournalValidation(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def journal_with(self, lines):
        run_dir = tempfile.mkdtemp(prefix="journal-coherence-")
        with open(os.path.join(run_dir, "journal.jsonl"), "w") as fh:
            fh.write("".join(lines))
        return self.m.Journal(run_dir)

    def test_completed_needs_key_and_mapping_result(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "good", "label": "a",'
            ' "result": {"ok": true, "cell": "A"}}\n',
            '{"type": "completed", "key": "good", "label": "a"}\n',
            '{"type": "completed", "key": "good", "label": "a", "result": [1]}\n',
            '{"type": "completed", "key": "good", "label": "a", "result": "x"}\n',
            '{"type": "completed", "key": ["good"], "label": "a",'
            ' "result": {"ok": true}}\n',
            '{"type": "completed", "key": 5, "label": "a",'
            ' "result": {"ok": true}}\n',
            '{"type": "completed", "key": "", "label": "a",'
            ' "result": {"ok": true}}\n',
            '{"type": "completed", "label": "a", "result": {"ok": true}}\n',
            '[1, 2]\n',
            '"garbage"\n',
            'not json\n',
        ])
        self.assertEqual(journal.cache, {"good": {"ok": True, "cell": "A"}})
        self.assertEqual(list(journal.steps), ["good"])

    def test_other_events_need_valid_keys_for_steps(self):
        journal = self.journal_with([
            '{"type": "failed", "key": ["k"], "label": "a", "error": "x"}\n',
            '{"type": "failed", "key": 7, "label": "b", "error": "x"}\n',
            '{"type": "failed", "label": "c", "error": "x"}\n',
            '{"type": "failed", "key": "bad", "label": "d", "error": "x"}\n',
        ])
        self.assertEqual(journal.cache, {})
        self.assertEqual(list(journal.steps), ["bad"])


class TestOrderedEligibility(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def mutant_module(self, old, new):
        source = SCRIPT.read_text()
        self.assertIn(old, source, "negative-generation mutation anchor drifted")
        path = pathlib.Path(tempfile.mkdtemp(prefix="journal-mutant-")) / "headless-workflow.py"
        path.write_text(source.replace(old, new, 1))
        return load_module(path)

    def journal_with(self, lines):
        run_dir = tempfile.mkdtemp(prefix="journal-coherence-")
        with open(os.path.join(run_dir, "journal.jsonl"), "w") as fh:
            fh.write("".join(lines))
        return self.m.Journal(run_dir)

    def test_stale_completion_skipped_until_cleared(self):
        run_dir = tempfile.mkdtemp(prefix="journal-coherence-")
        with open(os.path.join(run_dir, "journal.jsonl"), "w") as fh:
            fh.write('{"type": "completed", "key": "k-b", "label": "matrix-b",'
                     ' "result": {"ok": true, "cell": "B"}}\n')
        journal = self.m.Journal(run_dir)
        cached = journal.cache["k-b"]
        self.assertFalse(journal.cache_stale(cached))
        journal.write({"type": "cell-state", "cell": "B", "state": "failed",
                       "generation": 1, "label": "matrix-b"})
        self.assertTrue(journal.cache_stale(cached))
        journal.write({"type": "cell-state", "cell": "B", "state": "passed",
                       "generation": 2, "label": "matrix-b"})
        self.assertTrue(journal.cache_stale(cached, "k-b"),
                        "old completion stays older than the failure after clear")
        self.assertFalse(journal.cache_stale(cached),
                         "state-only compatibility check reflects current state")
        self.assertTrue(journal.cache_stale(["not", "a", "mapping"]))
        self.assertFalse(journal.cache_stale({"ok": True}))
        self.assertFalse(journal.cache_stale({"ok": True, "cell": "C"}))

    def test_delayed_lower_generation_never_regresses(self):
        run_dir = tempfile.mkdtemp(prefix="journal-coherence-")
        with open(os.path.join(run_dir, "journal.jsonl"), "w") as fh:
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": 4, "label": "matrix-b"}\n')
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": 1, "label": "matrix-b"}\n')
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": 4, "label": "matrix-b"}\n')
            fh.write('{"type": "cell-state", "cell": [], "state": "failed",'
                     ' "generation": 9}\n')
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": 9, "reason": ["bad"]}\n')
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": 9, "evidence_state": "maybe"}\n')
        journal = self.m.Journal(run_dir)
        self.assertEqual(journal._cell_state, {"B": ("failed", 4)})
        self.assertTrue(journal.cache_stale({"ok": True, "cell": "B"}))

    def test_negative_generation_never_invalidates_completion(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": -1, "label": "b"}\n',
        ])
        self.assertEqual(journal._cell_state, {})
        self.assertEqual(journal._cell_failures, {})
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))

    def test_malformed_generations_never_overwrite_good_state(self):
        journal = self.journal_with([
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 3, "label": "b"}\n',
        ])
        self.assertEqual(journal._cell_state, {"B": ("failed", 3)})
        bad = [-1, -2147483648, -9223372036854775808, True, False,
               1.5, "3", None, [3], {"n": 3}]
        for i, generation in enumerate(bad):
            journal._ingest({"type": "cell-state", "cell": "B",
                             "state": "failed", "generation": generation},
                            True, 100 + i)
        journal._ingest({"type": "cell-state", "cell": "B", "state": "failed",
                         "generation": 9, "reason": ["bad"]}, True, 200)
        journal._ingest({"type": "cell-state", "cell": "B", "state": "passed",
                         "generation": -1}, True, 201)
        self.assertEqual(journal._cell_state, {"B": ("failed", 3)})
        failure = journal._cell_failures.get("B")
        self.assertIsNotNone(failure)
        self.assertEqual(failure[0], 3)
        journal._ingest({"type": "cell-state", "cell": "B", "state": "passed",
                         "generation": 4}, True, 202)
        self.assertEqual(journal._cell_state, {"B": ("passed", 4)})

    def test_stale_completion_redispatched_on_resume(self):
        h = Harness()
        body = """
        META = {"name": "cell-b", "description": "cell-b"}
        async def main(wf, args):
            r = await wf.agent("review B @@REPLY:ok@@", route="glm",
                               label="review-b", cell="B")
            return {"ok": r.ok, "session": r.session_id, "cached": r.cached}
        """
        script = h.write_script(body)
        run_id = h.run_id_from(h.run("run", script))
        first = h.result(run_id)
        self.assertEqual(len(h.calls()), 1)
        run_dir = os.path.join(h.state, "runs", run_id)
        with open(os.path.join(run_dir, "journal.jsonl"), "a") as fh:
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": 1, "label": "review-b"}\n')
        h.run("run", script, "--resume", run_id)
        self.assertEqual(len(h.calls()), 2, "stale completion must not satisfy")
        second = h.result(run_id)
        self.assertNotEqual(second["session"], first["session"])
        with open(os.path.join(run_dir, "journal.jsonl"), "a") as fh:
            fh.write('{"type": "cell-state", "cell": "B", "state": "passed",'
                     ' "generation": 2, "label": "review-b"}\n')
        h.run("run", script, "--resume", run_id)
        self.assertEqual(len(h.calls()), 2, "cleared completion serves again")

    def test_negative_generation_completion_served_on_resume(self):
        h = Harness()
        body = """
        META = {"name": "cell-b", "description": "cell-b"}
        async def main(wf, args):
            r = await wf.agent("review B @@REPLY:ok@@", route="glm",
                               label="review-b", cell="B")
            return {"ok": r.ok, "session": r.session_id, "cached": r.cached}
        """
        script = h.write_script(body)
        run_id = h.run_id_from(h.run("run", script))
        first = h.result(run_id)
        self.assertEqual(len(h.calls()), 1)
        run_dir = os.path.join(h.state, "runs", run_id)
        with open(os.path.join(run_dir, "journal.jsonl"), "a") as fh:
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": -1, "label": "review-b"}\n')
        h.run("run", script, "--resume", run_id)
        self.assertEqual(
            len(h.calls()), 1,
            "negative-generation state must not invalidate the good completion")
        second = h.result(run_id)
        self.assertEqual(second["session"], first["session"])

    def test_mutation_negative_generation_guard_removed_invalidates_completion(self):
        mutant = self.mutant_module(
            "        if (not isinstance(generation, int) or isinstance(generation, bool)\n"
            "                or generation < 0):",
            "        if (not isinstance(generation, int) or isinstance(generation, bool)\n"
            "                or False):",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed", '
            '"generation": -1}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal._cell_state, {"B": ("failed", -1)})
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))


if __name__ == "__main__":
    unittest.main()
