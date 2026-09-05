"""Tests for headless-workflow: the orchestrator that runs a Python workflow
script over many headless-agent workers with journaling, resume, fork, routing
fallback, schema repair, and concurrency limits.

All tests run against tests/fake-headless-agent.sh so no provider is touched.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "headless-workflow.py"
FAKE = HERE / "fake-headless-agent.sh"


def load_module():
    spec = importlib.util.spec_from_file_location("headless_workflow", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Harness:
    """One isolated state dir + fake run root per test."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="hw-test-")
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
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body))
        return path

    def run(self, *cli, check=True, timeout=120):
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

    def result(self, run_id):
        proc = self.run("result", run_id)
        return json.loads(proc.stdout)


SIMPLE = """
META = {"name": "simple", "description": "one agent"}
async def main(wf, args):
    r = await wf.agent("Say hi @@REPLY:hello world@@", route="glm", label="hi")
    return {"text": r.text, "ok": r.ok, "route": r.route, "session": r.session_id}
"""


class TestRunBasics(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_simple_run_returns_text_and_journals(self):
        script = self.h.write_script(SIMPLE)
        proc = self.h.run("run", script)
        run_id = self.h.run_id_from(proc)
        res = self.h.result(run_id)
        self.assertEqual(res["text"], "hello world")
        self.assertTrue(res["ok"])
        self.assertEqual(res["route"], "glm")
        self.assertTrue(res["session"].startswith("sess-"))
        journal = pathlib.Path(self.h.state) / "runs" / run_id / "journal.jsonl"
        events = [json.loads(l) for l in journal.read_text().splitlines()]
        kinds = [e["type"] for e in events]
        self.assertIn("started", kinds)
        self.assertIn("completed", kinds)
        calls = self.h.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["harness"], "claude_code")
        self.assertEqual(calls[0]["model"], "glm-5.3-flash")
        self.assertEqual(calls[0]["label"], "hi")

    def test_args_are_passed_verbatim(self):
        script = self.h.write_script("""
        META = {"name": "args", "description": "echo args"}
        async def main(wf, args):
            return {"got": args}
        """)
        proc = self.h.run("run", script, "--args", '{"items": ["a", "b"], "n": 2}')
        res = self.h.result(self.h.run_id_from(proc))
        self.assertEqual(res["got"], {"items": ["a", "b"], "n": 2})

    def test_status_lists_steps(self):
        script = self.h.write_script(SIMPLE)
        run_id = self.h.run_id_from(self.h.run("run", script))
        proc = self.h.run("status", run_id)
        self.assertIn("hi", proc.stdout)
        self.assertIn("completed", proc.stdout)
        self.assertIn("finished", proc.stdout.lower())


class TestSchemaAndRepair(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_schema_parses_fenced_json(self):
        script = self.h.write_script("""
        META = {"name": "schema", "description": "fenced json"}
        SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
        async def main(wf, args):
            r = await wf.agent('give json @@JSON:{"a": 1}@@ @@FENCE@@', route="glm", schema=SCHEMA)
            return {"data": r.data, "ok": r.ok}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertEqual(res["data"], {"a": 1})
        self.assertTrue(res["ok"])

    def test_invalid_output_is_repaired_by_resuming_the_same_session(self):
        script = self.h.write_script("""
        META = {"name": "repair", "description": "repair via resume"}
        SCHEMA = {"type": "object", "required": ["a"]}
        async def main(wf, args):
            r = await wf.agent('give json @@INVALID_THEN_VALID:{"a": 2}@@', route="glm", schema=SCHEMA)
            return {"data": r.data, "ok": r.ok, "attempts": r.attempts}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertEqual(res["data"], {"a": 2})
        self.assertEqual(res["attempts"], 2)
        calls = self.h.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["resume"], "sess-1", "repair must resume the original session")

    def test_schema_failure_after_retries_yields_not_ok(self):
        script = self.h.write_script("""
        META = {"name": "bad", "description": "never valid"}
        SCHEMA = {"type": "object", "required": ["a"]}
        async def main(wf, args):
            r = await wf.agent('give json @@REPLY:nope@@', route="glm", schema=SCHEMA, retries=1)
            return {"data": r.data, "ok": r.ok, "error": r.error}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertIsNone(res["data"])
        self.assertFalse(res["ok"])
        self.assertIn("schema", res["error"].lower())
        self.assertEqual(len(self.h.calls()), 2)


class TestRoutingFallback(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_failed_route_falls_back_to_next_route(self):
        script = self.h.write_script("""
        META = {"name": "fallback", "description": "glm fails, muse succeeds"}
        async def main(wf, args):
            r = await wf.agent("work @@FAIL_IF_MODEL:glm-5.3-flash@@ @@REPLY:done@@", route="glm", fallback=["muse"])
            return {"text": r.text, "route": r.route, "ok": r.ok}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertTrue(res["ok"])
        self.assertEqual(res["route"], "muse")
        calls = self.h.calls()
        self.assertEqual([c["model"] for c in calls], ["glm-5.3-flash", "muse-spark-1.3-contributor"])

    def test_explicit_route_dict(self):
        script = self.h.write_script("""
        META = {"name": "explicit", "description": "explicit route"}
        async def main(wf, args):
            r = await wf.agent("x @@REPLY:y@@", route={"harness": "kimi_code", "provider": "kimi", "model": "k3", "effort": "max"})
            return {"route": r.route}
        """)
        self.h.result(self.h.run_id_from(self.h.run("run", script)))
        c = self.h.calls()[0]
        self.assertEqual((c["harness"], c["provider"], c["model"], c["effort"]), ("kimi_code", "kimi", "k3", "max"))

    def test_no_fallback_yields_not_ok(self):
        script = self.h.write_script("""
        META = {"name": "nofb", "description": "fail hard"}
        async def main(wf, args):
            r = await wf.agent("work @@FAIL_IF_MODEL:glm-5.3-flash@@", route="glm", fallback=[])
            return {"ok": r.ok, "error": r.error}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertFalse(res["ok"])
        self.assertIn("429", res["error"])


class TestCombinators(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_parallel_is_a_barrier_with_none_for_failures(self):
        script = self.h.write_script("""
        META = {"name": "par", "description": "parallel"}
        async def main(wf, args):
            rs = await wf.parallel([
                lambda: wf.agent("a @@REPLY:A@@", route="glm"),
                lambda: wf.agent("b @@FAIL_IF_MODEL:glm-5.3-flash@@", route="glm", fallback=[]),
                lambda: wf.agent("c @@REPLY:C@@", route="glm"),
            ])
            return [None if r is None or not r.ok else r.text for r in rs]
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertEqual(res, ["A", None, "C"])

    def test_pipeline_runs_stages_per_item_without_barrier(self):
        script = self.h.write_script("""
        META = {"name": "pipe", "description": "pipeline"}
        async def main(wf, args):
            async def stage1(item, i):
                return await wf.agent(f"s1 {item} @@REPLY:{item}1@@ @@SLEEP:{0.5 if i == 0 else 0}@@", route="glm", label=f"s1-{item}")
            async def stage2(prev, item, i):
                return await wf.agent(f"s2 {prev.text} @@REPLY:{prev.text}2@@", route="glm", label=f"s2-{item}")
            outs = await wf.pipeline(["x", "y"], stage1, stage2)
            return [o.text for o in outs]
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertEqual(res, ["x12", "y12"])
        labels = [c["label"] for c in self.h.calls()]
        # y's second stage must start before x's slow first stage finishes: no barrier
        self.assertLess(labels.index("s2-y"), labels.index("s2-x"))

    def test_pipeline_stage_exception_drops_item_to_none(self):
        script = self.h.write_script("""
        META = {"name": "pipe-err", "description": "pipeline error"}
        async def main(wf, args):
            async def s1(item, i):
                if item == "bad":
                    raise ValueError("boom")
                return await wf.agent(f"ok @@REPLY:{item}@@", route="glm")
            outs = await wf.pipeline(["good", "bad"], s1)
            return [None if o is None else o.text for o in outs]
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertEqual(res, ["good", None])


class TestFork(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_native_fork_shares_parent_session(self):
        script = self.h.write_script("""
        META = {"name": "fork", "description": "fork fan-out"}
        async def main(wf, args):
            parent = await wf.agent("build context @@REPLY:ctx@@", route="glm", label="parent")
            kids = await wf.parallel([
                lambda: wf.fork(parent, "child a @@ECHO_PARENT@@", label="a"),
                lambda: wf.fork(parent, "child b @@ECHO_PARENT@@", label="b"),
            ])
            return {"parent": parent.session_id, "kids": [(k.session_id, k.text, k.forked) for k in kids]}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertEqual(res["parent"], "sess-1")
        for sid, text, forked in res["kids"]:
            self.assertTrue(sid.startswith("fork-of-sess-1"))
            self.assertIn("parent=sess-1", text)
            self.assertTrue(forked)

    def test_fork_falls_back_to_context_injection_on_non_fork_harness(self):
        script = self.h.write_script("""
        META = {"name": "fork-fb", "description": "fork fallback"}
        async def main(wf, args):
            parent = await wf.agent("gather @@REPLY:THE_CONTEXT@@", route="gemini", label="parent")
            kid = await wf.fork(parent, "use it @@ECHO_PARENT@@", label="kid")
            return {"forked": kid.forked, "text": kid.text, "session": kid.session_id}
        """)
        res = self.h.result(self.h.run_id_from(self.h.run("run", script)))
        self.assertFalse(res["forked"])
        self.assertIn("parent=none", res["text"])
        calls = self.h.calls()
        kid_prompt = pathlib.Path(self.h.fake_root, f"run-{calls[1]['n']:04d}", "prompt.txt").read_text()
        self.assertIn("THE_CONTEXT", kid_prompt, "fallback fork must inject the parent's answer into the child prompt")
        self.assertIn("gather", kid_prompt, "fallback fork must inject the parent's prompt into the child prompt")


class TestResume(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_resume_reuses_cached_steps_and_reruns_changed_ones(self):
        body = """
        META = {"name": "resume", "description": "resume"}
        async def main(wf, args):
            a = await wf.agent("first @@REPLY:one@@", route="glm", label="first")
            b = await wf.agent("second %s @@REPLY:two@@", route="glm", label="second")
            return [a.text, b.text]
        """
        script = self.h.write_script(body % "v1")
        run_id = self.h.run_id_from(self.h.run("run", script))
        self.assertEqual(len(self.h.calls()), 2)
        # identical rerun: zero dispatches
        self.h.run("run", script, "--resume", run_id)
        self.assertEqual(len(self.h.calls()), 2)
        self.assertEqual(self.h.result(run_id), ["one", "two"])
        # change the second prompt: exactly one new dispatch
        script2 = self.h.write_script(body % "v2", name="wf2.py")
        self.h.run("run", script2, "--resume", run_id)
        self.assertEqual(len(self.h.calls()), 3)
        self.assertEqual(self.h.calls()[2]["label"], "second")


class TestLimits(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_route_concurrency_limit_is_enforced(self):
        script = self.h.write_script("""
        META = {"name": "conc", "description": "concurrency"}
        async def main(wf, args):
            rs = await wf.parallel([lambda i=i: wf.agent(f"n{i} @@SLEEP:0.6@@ @@REPLY:{i}@@", route="glm", label=f"n{i}") for i in range(4)])
            return [r.text for r in rs]
        """)
        routes = self.h.write_script('{"glm": {"max_concurrency": 2}}', name="routes.json")
        t0 = time.time()
        self.h.result(self.h.run_id_from(self.h.run("run", script, "--routes", routes)))
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 1.1, "four 0.6 s agents at concurrency 2 need at least two waves")
        calls = self.h.calls()
        starts = sorted(c["start"] for c in calls)
        ends = sorted(c["end"] for c in calls)
        # at most 2 overlapping at any start instant
        for s in starts:
            running = sum(1 for c in calls if c["start"] <= s < c["end"])
            self.assertLessEqual(running, 2)

    def test_max_agents_cap_fails_the_run(self):
        script = self.h.write_script("""
        META = {"name": "cap", "description": "cap"}
        async def main(wf, args):
            for i in range(3):
                await wf.agent(f"n{i} @@REPLY:{i}@@", route="glm")
            return "unreachable"
        """)
        proc = self.h.run("run", script, "--max-agents", "2", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("max-agents", (proc.stdout + proc.stderr))
        self.assertEqual(len(self.h.calls()), 2)


class TestUnits(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def test_extract_json_variants(self):
        ex = self.m.extract_json
        self.assertEqual(ex('{"a": 1}'), {"a": 1})
        self.assertEqual(ex('Sure:\n```json\n{"a": [1, 2]}\n```\nbye'), {"a": [1, 2]})
        self.assertEqual(ex('prefix text {"a": {"b": 2}} trailing'), {"a": {"b": 2}})
        self.assertEqual(ex('[1, 2, 3]'), [1, 2, 3])
        self.assertIsNone(ex('no json here'))

    def test_journal_skips_valid_json_non_mapping_garbage(self):
        with tempfile.TemporaryDirectory(prefix="journal-unit-") as run_dir:
            journal_path = pathlib.Path(run_dir, "journal.jsonl")
            journal_path.write_text(
                '[1, 2]\n"garbage"\n'
                '{"type":"completed","key":"good","result":{"ok":true}}\n',
                encoding="utf-8",
            )
            journal = self.m.Journal(run_dir)
            self.assertEqual(journal.cache["good"], {"ok": True})
            self.assertEqual(journal.steps["good"]["type"], "completed")

    def test_validate_schema(self):
        v = self.m.validate_schema
        schema = {"type": "object", "required": ["a", "b"], "properties": {
            "a": {"type": "integer"}, "b": {"type": "array", "items": {"type": "string"}},
            "c": {"type": "string", "enum": ["x", "y"]}}}
        self.assertIsNone(v({"a": 1, "b": ["s"]}, schema))
        self.assertIn("required", v({"a": 1}, schema))
        self.assertIn("type", v({"a": "1", "b": []}, schema))
        self.assertIn("enum", v({"a": 1, "b": [], "c": "z"}, schema))
        self.assertIn("items", v({"a": 1, "b": [1]}, schema))

    def test_step_key_is_stable_and_sensitive(self):
        k = self.m.step_key
        a = k("agent", "prompt", "glm", {}, None, None)
        self.assertEqual(a, k("agent", "prompt", "glm", {}, None, None))
        self.assertNotEqual(a, k("agent", "prompt2", "glm", {}, None, None))
        self.assertNotEqual(a, k("agent", "prompt", "muse", {}, None, None))
        self.assertNotEqual(a, k("agent", "prompt", "glm", {}, None, "parent-session"))

    def test_default_routes_cover_documented_names(self):
        routes = self.m.load_routes(None)
        for name in ["glm", "gemini", "muse", "kimi", "opus", "sonnet", "luna", "sol"]:
            self.assertIn(name, routes)
            self.assertIn("harness", routes[name])
            self.assertIn("provider", routes[name])
            self.assertIn("model", routes[name])
        self.assertFalse(self.m.route_supports_fork(routes["gemini"]))
        self.assertTrue(self.m.route_supports_fork(routes["glm"]))


if __name__ == "__main__":
    unittest.main()
