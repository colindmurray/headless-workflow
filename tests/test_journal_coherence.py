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

    def test_missing_generation_never_invalidates_completion(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "label": "b"}\n',
        ])
        self.assertEqual(journal._cell_state, {})
        self.assertEqual(journal._cell_failures, {})
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))

    def test_explicit_zero_generation_accepted(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 0, "label": "b"}\n',
        ])
        self.assertEqual(journal._cell_state, {"B": ("failed", 0)})
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))

    def test_cell_state_never_enters_steps(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 1, "key": "k", "label": "b"}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": -1, "key": "poison", "label": "b"}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "key": "poison-missing", "label": "b"}\n',
        ])
        self.assertEqual(list(journal.steps), ["k"])
        self.assertEqual(journal._cell_state, {"B": ("failed", 1)})
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))

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
        journal._ingest({"type": "cell-state", "cell": "B", "state": "failed"},
                        True, 201)
        journal._ingest({"type": "cell-state", "cell": "B", "state": "passed",
                         "generation": -1}, True, 202)
        journal._ingest({"type": "cell-state", "cell": "B", "state": "failed",
                         "key": "poison"}, True, 203)
        self.assertEqual(journal._cell_state, {"B": ("failed", 3)})
        failure = journal._cell_failures.get("B")
        self.assertIsNotNone(failure)
        self.assertEqual(failure[0], 3)
        self.assertNotIn("poison", journal.steps)
        journal._ingest({"type": "cell-state", "cell": "B", "state": "passed",
                         "generation": 4}, True, 204)
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

    def test_missing_generation_completion_served_on_resume(self):
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
                     ' "label": "review-b"}\n')
            fh.write('{"type": "cell-state", "cell": "B", "state": "failed",'
                     ' "generation": -1, "key": "poison",'
                     ' "label": "review-b"}\n')
        h.run("run", script, "--resume", run_id)
        self.assertEqual(
            len(h.calls()), 1,
            "missing/negative state must not invalidate the good completion")
        second = h.result(run_id)
        self.assertEqual(second["session"], first["session"])

    def test_atomic_acceptance_equal_failure_ignored(self):
        journal = self.journal_with([
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 1}\n',
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "passed",'
            ' "generation": 2}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 2}\n',
        ])
        self.assertEqual(journal._cell_state, {"B": ("passed", 2)})
        self.assertEqual(list(journal._cell_failures), ["B"])
        self.assertEqual(journal._cell_failures["B"][0], 1)
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))
        self.assertEqual(journal._sequence, 3)

    def test_atomic_acceptance_lower_failure_ignored(self):
        journal = self.journal_with([
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 1}\n',
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "passed",'
            ' "generation": 3}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 2}\n',
        ])
        self.assertEqual(journal._cell_state, {"B": ("passed", 3)})
        self.assertEqual(journal._cell_failures["B"][0], 1)
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))
        self.assertEqual(journal._sequence, 3)

    def test_ordering_matrix(self):
        # The completion leads every row, so any recorded failure makes it
        # stale; the atomicity signal is the failure generation, which must
        # never advance past the accepted state generation. Freshness past
        # an ignored failure needs a completion earned after it -- covered
        # by the atomic acceptance tests above.
        cases = [
            ("higher failed advances",
             [("failed", 1)], ("failed", 1), 1, True),
            ("higher passed clears state only",
             [("failed", 1), ("passed", 2)], ("passed", 2), 1, True),
            ("equal failed ignored",
             [("failed", 1), ("passed", 2), ("failed", 2)],
             ("passed", 2), 1, True),
            ("lower failed ignored",
             [("failed", 1), ("passed", 3), ("failed", 2)],
             ("passed", 3), 1, True),
            ("exact duplicate ignored",
             [("failed", 2), ("failed", 2)], ("failed", 2), 2, True),
            ("equal passed keeps first failure",
             [("failed", 2), ("passed", 2)], ("failed", 2), 2, True),
            ("equal failed keeps first pass",
             [("passed", 2), ("failed", 2)], ("passed", 2), None, False),
            ("delayed lower passed ignored",
             [("failed", 3), ("passed", 1)], ("failed", 3), 3, True),
            ("higher failed after pass advances",
             [("passed", 1), ("failed", 2)], ("failed", 2), 2, True),
        ]
        for name, events, state, failure_gen, keyed_stale in cases:
            with self.subTest(name=name):
                lines = ['{"type": "completed", "key": "k", "label": "b",'
                         ' "result": {"ok": true, "cell": "B"}}\n']
                for cell_state, generation in events:
                    lines.append(
                        '{"type": "cell-state", "cell": "B", "state": "%s",'
                        ' "generation": %d}\n' % (cell_state, generation))
                journal = self.journal_with(lines)
                self.assertEqual(journal._cell_state, {"B": state}, name)
                if failure_gen is None:
                    self.assertEqual(journal._cell_failures, {}, name)
                else:
                    self.assertEqual(
                        journal._cell_failures["B"][0], failure_gen, name)
                self.assertEqual(
                    journal.cache_stale(journal.cache["k"], "k"),
                    keyed_stale, name)

    def test_keyed_invalid_types_rejected_from_steps(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"cell": "B", "state": "failed", "generation": 3,'
            ' "key": "poison"}\n',
            '{"type": "failed", "cell": "B", "state": "failed",'
            ' "generation": 3, "key": "poison"}\n',
            '{"type": null, "cell": "B", "state": "failed",'
            ' "generation": 3, "key": "poison"}\n',
            '{"type": 5, "cell": "B", "state": "failed",'
            ' "generation": 3, "key": "poison"}\n',
            '{"type": "cell-state", "cell": [], "state": "failed",'
            ' "generation": 9, "key": "poison"}\n',
            '{"type": "cell-state", "cell": "B", "state": "bogus",'
            ' "generation": 9, "key": "poison"}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": -1, "key": "poison"}\n',
            '{"type": null, "key": "poison"}\n',
            '{"type": 5, "key": "poison"}\n',
        ])
        self.assertEqual(list(journal.steps), ["k"])
        self.assertEqual(journal._cell_state, {})
        self.assertEqual(journal._cell_failures, {})
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))
        self.assertEqual(journal._sequence, 1)
        self.assertEqual(journal._cache_sequence, {"k": 1})

    def test_replay_sequence_counts_only_accepted(self):
        journal = self.journal_with([
            '{"type": "completed", "key": "k", "label": "b",'
            ' "result": {"ok": true, "cell": "B"}}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed"}\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 1}\n',
            'not json\n',
            '[1, 2]\n',
            '{"type": "cell-state", "cell": "B", "state": "failed",'
            ' "generation": 1}\n',
            '{"type": "cell-state", "cell": "B", "state": "passed",'
            ' "generation": 2}\n',
        ])
        self.assertEqual(journal._sequence, 3)
        self.assertEqual(journal._cache_sequence, {"k": 1})
        self.assertEqual(journal._cell_state, {"B": ("passed", 2)})
        self.assertEqual(journal._cell_failures, {"B": (1, 2)})
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))
        self.assertFalse(journal.cache_stale(journal.cache["k"]))

    def test_repeated_malformed_replay_leaves_sequence(self):
        lines = ['{"type": "completed", "key": "k", "label": "b",'
                 ' "result": {"ok": true, "cell": "B"}}\n']
        lines += ['{"type": "cell-state", "cell": "B", "state": "failed"}\n'] * 3
        journal = self.journal_with(lines)
        self.assertEqual(journal._sequence, 1)
        self.assertEqual(journal._cell_state, {})
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))

    def test_legitimate_generic_events_retained(self):
        run_dir = tempfile.mkdtemp(prefix="journal-coherence-")
        journal = self.m.Journal(run_dir)
        self.assertTrue(journal._ingest(
            {"type": "phase", "title": "p"}, False, 99))
        self.assertTrue(journal._ingest(
            {"type": "started", "key": "s", "label": "a"}, False, 99))
        self.assertTrue(journal._ingest(
            {"type": "failed", "key": "bad", "label": "a", "error": "x"},
            False, 99))
        self.assertTrue(journal._ingest(
            {"type": "run-started", "key": "rs", "resumed": False},
            False, 99))
        self.assertTrue(journal._ingest(
            {"type": "run-finished", "key": "rf", "status": "ok",
             "dispatched": 1}, False, 99))
        self.assertTrue(journal._ingest(
            {"type": "completed", "key": "good", "label": "a",
             "result": {"ok": True}}, False, 99))
        self.assertEqual(
            sorted(journal.steps), ["bad", "good", "rf", "rs", "s"])
        self.assertTrue(journal._ingest({"type": "phase"}, False, 99))
        self.assertFalse(journal._ingest({"key": "orphan"}, False, 99))

    def test_legitimate_projectionless_generics_advance_sequence(self):
        run_dir = tempfile.mkdtemp(prefix="journal-coherence-")
        journal = self.m.Journal(run_dir)
        journal.write({"type": "phase", "title": "p"})
        journal.write({"type": "run-started", "resumed": False})
        journal.write({"type": "run-finished", "status": "ok"})
        self.assertEqual(journal._sequence, 3)
        self.assertEqual(journal.steps, {})

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

    def test_mutation_generation_presence_removed_accepts_missing(self):
        mutant = self.mutant_module(
            '        if "generation" not in event:\n'
            "            return False\n"
            '        generation = event.get("generation")',
            '        generation = event.get("generation", 0)',
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed"}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal._cell_state, {"B": ("failed", 0)})
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))

    def test_mutation_bool_exclusion_removed_accepts_bool(self):
        mutant = self.mutant_module(
            "        if (not isinstance(generation, int) or isinstance(generation, bool)\n"
            "                or generation < 0):",
            "        if (not isinstance(generation, int) or False\n"
            "                or generation < 0):",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed", '
            '"generation": true}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertIn("B", journal._cell_state)
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))

    def test_mutation_cell_state_step_branch_restored_poisons_steps(self):
        mutant = self.mutant_module(
            '        if kind == "cell-state" or "cell" in event or "generation" in event:\n'
            "            return self._note_cell_state(event, sequence)\n",
            "        if False:  # MUTANT: classification removed\n"
            "            pass\n",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "failed", "cell": "B", "state": "failed", '
            '"generation": -1, "key": "poison"}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertIn("poison", journal.steps)

    def test_mutation_metadata_check_reordered_mutates_before_validate(self):
        mutant = self.mutant_module(
            '        if "label" in event and not isinstance(event["label"], str):\n'
            "            return False\n",
            "        self._cell_state[cell] = (state, generation)  # MUTANT: premature\n"
            '        if "label" in event and not isinstance(event["label"], str):\n'
            "            return False\n",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "cell-state", "cell": "B", "state": "failed", '
            '"generation": 9, "reason": ["bad"]}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal._cell_state, {"B": ("failed", 9)})

    def test_mutation_partial_acceptance_state_without_failure(self):
        mutant = self.mutant_module(
            "        self._cell_state[cell] = (state, generation)\n"
            '        if state == "failed":\n'
            "            self._cell_failures[cell] = (generation, sequence)\n"
            "        return True",
            "        self._cell_state[cell] = (state, generation)\n"
            "        return True  # MUTANT: failure projection dropped",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed", '
            '"generation": 1}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal._cell_state, {"B": ("failed", 1)})
        self.assertEqual(journal._cell_failures, {})
        self.assertFalse(journal.cache_stale(journal.cache["k"], "k"))

    def test_mutation_split_ordering_failure_without_gate(self):
        mutant = self.mutant_module(
            "        current = self._cell_state.get(cell)\n"
            "        if current is not None and generation <= current[1]:\n"
            "            return False\n"
            "        self._cell_state[cell] = (state, generation)\n"
            '        if state == "failed":\n'
            "            self._cell_failures[cell] = (generation, sequence)\n"
            "        return True",
            "        current = self._cell_state.get(cell)\n"
            "        if current is None or generation > current[1]:\n"
            "            self._cell_state[cell] = (state, generation)\n"
            '        if state == "failed":  # MUTANT: split ordering\n'
            "            failure = self._cell_failures.get(cell)\n"
            "            if failure is None or generation > failure[0]:\n"
            "                self._cell_failures[cell] = (generation, sequence)\n"
            "        return True",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed", '
            '"generation": 1}\n'
            '{"type": "cell-state", "cell": "B", "state": "passed", '
            '"generation": 2}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed", '
            '"generation": 2}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal._cell_state, {"B": ("passed", 2)})
        self.assertEqual(journal._cell_failures["B"][0], 2)
        self.assertTrue(journal.cache_stale(journal.cache["k"], "k"))

    def test_mutation_replay_sequence_unconditional(self):
        mutant = self.mutant_module(
            "                    if self._ingest(e, allow_cache=True,\n"
            "                                    sequence=self._sequence + 1):\n"
            "                        self._sequence += 1",
            "                    self._ingest(e, allow_cache=True,  # MUTANT\n"
            "                                 sequence=self._sequence + 1)\n"
            "                    self._sequence += 1",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "completed", "key": "k", "result": '
            '{"ok": true, "cell": "B"}}\n'
            '{"type": "cell-state", "cell": "B", "state": "failed"}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal._sequence, 2)

    def test_mutation_overbroad_rejection_drops_generic(self):
        mutant = self.mutant_module(
            "        if valid_cache_key(key):\n"
            "            self.steps[key] = event\n"
            "            return True\n"
            "        return False",
            "        if False:  # MUTANT: generic projection dropped\n"
            "            self.steps[key] = event\n"
            "        return False",
        )
        run_dir = tempfile.mkdtemp(prefix="journal-mutant-run-")
        journal_path = pathlib.Path(run_dir) / "journal.jsonl"
        journal_path.write_text(
            '{"type": "failed", "key": "bad", "label": "a", "error": "x"}\n'
        )
        journal = mutant.Journal(run_dir)
        self.assertEqual(journal.steps, {})


if __name__ == "__main__":
    unittest.main()
