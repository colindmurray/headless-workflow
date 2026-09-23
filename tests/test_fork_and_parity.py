"""Regression tests for fork placement, session-safe fallback, per-occurrence
resume, fatal errors inside parallel(), interrupt cleanup, and the built-in
Workflow parity additions (phase=, isolation, workflow(), loose thunks)."""
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest

from test_headless_workflow import SCRIPT, Harness


def calls(h):
    p = os.path.join(h.fake_root, "calls.log")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def dirs(h):
    return {d["n"]: d["dir"] for d in (json.loads(l) for l in open(os.path.join(h.fake_root, "dirs.log")))}


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        os.environ["HEADLESS_WORKFLOW_SESSION_RETRY_DELAY"] = "0"

    def tearDown(self):
        os.environ.pop("HEADLESS_WORKFLOW_SESSION_RETRY_DELAY", None)

    def run_wf(self, body, *extra, check=True):
        script = self.h.write_script(body)
        proc = self.h.run("run", script, *extra, check=check)
        return proc, self.h.run_id_from(proc)

    def result(self, run_id):
        return json.loads(self.h.run("result", run_id).stdout)


class TestForkPlacement(Base):
    def test_fork_inherits_parent_dir_so_claude_code_finds_the_session(self):
        _, rid = self.run_wf(f"""
        META = {{"name": "fdir", "description": "d"}}
        async def main(wf, args):
            p = await wf.agent("explore @@REPLY:READY@@", route="glm", dir={self.h.tmp!r}, posture="code", label="p")
            k = await wf.fork(p, "child @@ECHO_PARENT@@", label="k")
            return {{"ok": k.ok, "forked": k.forked, "error": k.error}}
        """)
        self.assertEqual(self.result(rid), {"ok": True, "forked": True, "error": None})
        c = calls(self.h)
        self.assertEqual(c[1]["posture"], "code")
        self.assertEqual(dirs(self.h)[2], self.h.tmp)

    def test_dict_route_parent_forks_natively(self):
        _, rid = self.run_wf("""
        META = {"name": "fdict", "description": "d"}
        R = {"harness": "claude_code", "provider": "zai", "model": "glm-x"}
        async def main(wf, args):
            p = await wf.agent("explore", route=R, label="p")
            k = await wf.fork(p, "child", label="k")
            return {"ok": k.ok, "forked": k.forked}
        """)
        self.assertEqual(self.result(rid), {"ok": True, "forked": True})
        self.assertTrue(calls(self.h)[1]["fork"])

    def test_fork_never_falls_back_to_another_route(self):
        # pi-glm's fallbacks are glm (another harness) and pi-muse (same harness,
        # another provider): neither shares the session's cache
        _, rid = self.run_wf("""
        META = {"name": "fpi", "description": "d"}
        async def main(wf, args):
            p = await wf.agent("explore", route="pi-glm", label="p")
            k = await wf.fork(p, "child @@FAIL_IF_MODEL:glm-5.3-flash@@", label="k")
            return {"ok": k.ok, "error": k.error}
        """)
        res = self.result(rid)
        self.assertFalse(res["ok"])
        self.assertIn("429", res["error"])
        forks = [c for c in calls(self.h) if c["fork"]]
        self.assertEqual([(c["harness"], c["provider"]) for c in forks], [("pi", "zai")] * 2, "one try plus one retry")

    def test_resume_never_crosses_harness_and_keeps_the_real_error(self):
        _, rid = self.run_wf("""
        META = {"name": "rsm", "description": "d"}
        async def main(wf, args):
            p = await wf.agent("start", route="glm", label="p")
            r = await wf.agent("go on @@FAIL_IF_MODEL:glm-5.3-flash@@", route="glm", resume=p.session_id, label="r")
            return {"ok": r.ok, "error": r.error}
        """)
        res = self.result(rid)
        self.assertFalse(res["ok"])
        self.assertIn("429", res["error"])
        self.assertEqual({c["harness"] for c in calls(self.h)}, {"claude_code"})

    def test_transient_fork_failure_retries_the_same_route(self):
        _, rid = self.run_wf("""
        META = {"name": "fretry", "description": "d"}
        async def main(wf, args):
            p = await wf.agent("explore", route="glm", label="p")
            k = await wf.fork(p, "child @@FAIL_FIRST_FORK@@", label="k")
            return {"ok": k.ok, "attempts": k.attempts}
        """)
        self.assertEqual(self.result(rid), {"ok": True, "attempts": 2})

    def test_require_native_fails_instead_of_degrading(self):
        _, rid = self.run_wf("""
        META = {"name": "fnat", "description": "d"}
        async def main(wf, args):
            p = await wf.agent("explore", route="gemini", label="p")
            k = await wf.fork(p, "child", require_native=True, label="k")
            return {"ok": k.ok, "error": k.error}
        """)
        res = self.result(rid)
        self.assertFalse(res["ok"])
        self.assertIn("native fork impossible", res["error"])
        self.assertEqual(len(calls(self.h)), 1)

    def test_fork_of_an_isolated_parent_whose_worktree_was_removed_fails_clearly(self):
        repo = os.path.join(self.h.tmp, "repo")
        os.makedirs(repo)
        for cmd in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"]):
            subprocess.run(["git", "-C", repo, *cmd], check=True)
        _, rid = self.run_wf(f"""
        META = {{"name": "fiso", "description": "d"}}
        async def main(wf, args):
            p = await wf.agent("explore", route="glm", dir={repo!r}, isolation="worktree", label="p")
            k = await wf.fork(p, "child", label="k")
            return {{"ok": k.ok, "error": k.error}}
        """)
        res = self.result(rid)
        self.assertFalse(res["ok"])
        self.assertIn("no longer exists", res["error"])
        self.assertEqual(len(calls(self.h)), 1)

    def test_fork_of_failed_parent_is_a_failed_result_not_an_exception(self):
        _, rid = self.run_wf("""
        META = {"name": "ffail", "description": "d"}
        async def main(wf, args):
            p = await wf.agent("x @@FAIL_IF_MODEL:sonnet@@", route="sonnet", label="p")
            k = await wf.fork(p, "child", label="k")
            return {"ok": k.ok, "error": k.error}
        """)
        res = self.result(rid)
        self.assertFalse(res["ok"])
        self.assertIn("successful parent", res["error"])


class TestResumeIdentity(Base):
    VOTERS = """
    META = {"name": "voters", "description": "d"}
    async def main(wf, args):
        vs = await wf.parallel([lambda: wf.agent("refute X", route="sonnet") for _ in range(3)])
        return [v.ok for v in vs]
    """

    def test_identical_calls_get_distinct_steps_and_all_resume_cached(self):
        _, rid = self.run_wf(self.VOTERS)
        journal = os.path.join(self.h.state, "runs", rid, "journal.jsonl")
        with open(journal) as fh:
            keys = {e["key"] for e in map(json.loads, fh) if e["type"] == "completed"}
        self.assertEqual(len(keys), 3)
        script = self.h.write_script(self.VOTERS)
        proc = self.h.run("run", script, "--resume", rid)
        self.assertIn("(0 dispatches)", proc.stdout)
        self.assertEqual(self.result(rid), [True, True, True])

    def test_resume_without_args_reuses_the_original_args(self):
        body = """
        META = {"name": "argy", "description": "d"}
        async def main(wf, args):
            r = await wf.agent("say " + args["w"], route="sonnet")
            return args["w"]
        """
        _, rid = self.run_wf(body, "--args", '{"w": "hi"}')
        meta = json.load(open(os.path.join(self.h.state, "runs", rid, "run.json")))
        proc = self.h.run("run", self.h.write_script(body), "--resume", rid)
        self.assertIn("(0 dispatches)", proc.stdout)
        after = json.load(open(os.path.join(self.h.state, "runs", rid, "run.json")))
        self.assertEqual(after["args"], {"w": "hi"})
        self.assertEqual(after["started_at"], meta["started_at"])
        self.assertEqual(len(after["resumes"]), 1)

    def test_unknown_resume_id_is_refused(self):
        proc = self.h.run("run", self.h.write_script(self.VOTERS), "--resume", "nope", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no such run", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.h.state, "runs", "nope")))

    def test_result_refuses_a_failed_run(self):
        proc, rid = self.run_wf("""
        META = {"name": "boom", "description": "d"}
        async def main(wf, args):
            raise RuntimeError("boom")
        """, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertNotEqual(self.h.run("result", rid, check=False).returncode, 0)


class TestFatalAndLifecycle(Base):
    def test_max_agents_is_a_hard_cap_inside_parallel_and_fails_the_run(self):
        proc, rid = self.run_wf("""
        META = {"name": "cap", "description": "d"}
        async def main(wf, args):
            return await wf.parallel([lambda i=i: wf.agent(f"t{i}", route="sonnet") for i in range(6)])
        """, "--max-agents", "2", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("max-agents", proc.stderr)
        self.assertLessEqual(len(calls(self.h)), 2)
        journal = [json.loads(l) for l in open(os.path.join(self.h.state, "runs", rid, "journal.jsonl"))]
        started = {e["key"] for e in journal if e["type"] == "started"}
        ended = {e["key"] for e in journal if e["type"] in ("completed", "failed")}
        self.assertEqual(started, ended, "every started step must end with completed or failed")

    def test_sigterm_kills_workers_and_marks_the_run_interrupted(self):
        script = self.h.write_script("""
        META = {"name": "slow", "description": "d"}
        async def main(wf, args):
            return await wf.agent("wait @@SLEEP:30@@", route="sonnet", label="slow")
        """)
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "run", script], env=self.h.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 10
        while time.time() < deadline and not list(pathlib.Path(self.h.fake_root).glob("run-*")):
            time.sleep(0.1)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 130)
        rid = self.h.run_id_from(subprocess.CompletedProcess([], 0, out, ""))
        meta = json.load(open(os.path.join(self.h.state, "runs", rid, "run.json")))
        self.assertEqual(meta["status"], "interrupted")
        time.sleep(0.5)
        self.assertEqual(calls(self.h), [], "the worker must be killed before it finishes")

    def test_timeout_kills_the_whole_process_group(self):
        from test_headless_workflow import load_module
        m = load_module()
        # the child spawns a grandchild in the same group, like a provider script's nohup'd worker
        proc = subprocess.Popen(["bash", "-c", "sleep 30 & echo $!; wait"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        t0 = time.time()
        self.assertEqual(m._communicate(proc, 0.5), (None, None))
        self.assertLess(time.time() - t0, 10)
        deadline = time.time() + 8  # killed members may linger briefly as zombies until init reaps them
        while time.time() < deadline and m._group_alive(proc.pid):
            time.sleep(0.1)
        self.assertFalse(m._group_alive(proc.pid))

    def test_provider_error_shapes(self):
        from test_headless_workflow import load_module
        rx = load_module().PROVIDER_ERROR_RE
        for err in ["API Error: Request rejected (429) rate limit", "API Error: 529 overloaded_error",
                    "503 Service Unavailable", "[API Error: 500 Internal Server Error]", "Usage limit reached for this plan",
                    "No output produced", "Rate limit exceeded, retry later"]:
            self.assertTrue(rx.search(err), err)
        for answer in ["142 files changed across 9 packages.", "Error: config.yaml missing key 'port' (line 3).",
                       "250 lines reviewed, no defects found.", "Unauthorized callers are rejected; no findings.",
                       "Overloaded operators are not used anywhere.", "The default quota window is 5h.",
                       '{"line": 502, "note": "missing timeout"}', "200 OK is returned by the health endpoint."]:
            self.assertFalse(rx.search(answer), answer)

    def test_nohup_ignored_sighup_stays_ignored(self):
        script = self.h.write_script("""
        META = {"name": "hup", "description": "d"}
        async def main(wf, args):
            return (await wf.agent("wait @@SLEEP:1.5@@ @@REPLY:done@@", route="sonnet")).text
        """)
        proc = subprocess.Popen(["nohup", sys.executable, str(SCRIPT), "run", script], env=self.h.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.h.tmp)
        deadline = time.time() + 10
        while time.time() < deadline and not list(pathlib.Path(self.h.fake_root).glob("run-*")):
            time.sleep(0.1)
        proc.send_signal(signal.SIGHUP)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)

    def test_short_valid_answer_mentioning_timeout_is_not_a_provider_error(self):
        _, rid = self.run_wf("""
        META = {"name": "heur", "description": "d"}
        S = {"type": "object", "required": ["line"]}
        async def main(wf, args):
            r = await wf.agent('@@JSON:{"line": 502, "note": "missing timeout"}@@', route="sonnet", schema=S)
            return {"ok": r.ok, "attempts": r.attempts}
        """)
        self.assertEqual(self.result(rid), {"ok": True, "attempts": 1})

    def test_repair_without_a_session_resends_the_task(self):
        _, rid = self.run_wf("""
        META = {"name": "nosess", "description": "d"}
        S = {"type": "object", "required": ["a"]}
        async def main(wf, args):
            r = await wf.agent("ORIGINAL-TASK @@NOSESSION@@", route="sonnet", schema=S, retries=1)
            return r.key
        """)
        key = self.result(rid)
        second = open(os.path.join(self.h.state, "runs", rid, "steps", key, "prompt-2.txt")).read()
        self.assertIn("ORIGINAL-TASK", second)
        self.assertIn("output contract", second)


class TestParity(Base):
    def test_parallel_accepts_coroutines_and_sync_thunks(self):
        _, rid = self.run_wf("""
        META = {"name": "loose", "description": "d"}
        async def main(wf, args):
            rs = await wf.parallel([wf.agent("a", route="sonnet"), lambda: 7, lambda: wf.agent("b", route="sonnet")])
            return [bool(rs[0]), rs[1], bool(rs[2])]
        """)
        self.assertEqual(self.result(rid), [True, 7, True])

    def test_per_call_phase_groups_status(self):
        _, rid = self.run_wf("""
        META = {"name": "ph", "description": "d"}
        async def main(wf, args):
            async def one(i):
                a = await wf.agent(f"find {i}", route="sonnet", phase="Find", label=f"find-{i}")
                return await wf.agent(f"verify {i}", route="sonnet", phase="Verify", label=f"verify-{i}")
            return await wf.pipeline([0, 1], lambda item, i: one(item))
        """)
        out = self.h.run("status", rid).stdout
        for line in out.splitlines():
            if "verify-" in line or "find-" in line:
                kind = "Verify" if "verify-" in line else "Find"
                heading = [l for l in out[:out.index(line)].splitlines() if l.startswith("== ")][-1]
                self.assertEqual(heading, f"== {kind}", line)

    def test_workflow_runs_a_child_script_one_level_deep(self):
        child = self.h.write_script("""
        META = {"name": "child", "description": "d"}
        async def main(wf, args):
            r = await wf.agent("child " + args, route="sonnet")
            if args == "outer":
                try:
                    await wf.workflow("child.py", "inner")
                except Exception as e:
                    return type(e).__name__
            return r.ok
        """, name="child.py")
        _, rid = self.run_wf("""
        META = {"name": "parent", "description": "d"}
        async def main(wf, args):
            return await wf.workflow("child.py", "outer")
        """)
        self.assertEqual(self.result(rid), "WorkflowError")

    def test_worktree_isolation_runs_in_a_fresh_worktree_and_removes_it_unchanged(self):
        repo = os.path.join(self.h.tmp, "repo")
        os.makedirs(os.path.join(repo, "sub"))
        pathlib.Path(repo, "sub", "f.txt").write_text("x")
        for cmd in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i"]):
            subprocess.run(["git", "-C", repo, *cmd], check=True)
        _, rid = self.run_wf(f"""
        META = {{"name": "iso", "description": "d"}}
        async def main(wf, args):
            r = await wf.agent("look", route="sonnet", dir={os.path.join(repo, "sub")!r}, isolation="worktree")
            return {{"ok": r.ok, "worktree": r.worktree}}
        """)
        self.assertEqual(self.result(rid), {"ok": True, "worktree": None})
        used = dirs(self.h)[1]
        self.assertTrue(used.endswith(os.sep + "sub"), "a subdirectory dir maps into the worktree")
        self.assertIn(os.path.join("runs", rid, "worktrees"), used)
        self.assertFalse(os.path.exists(used), "an unchanged worktree is removed")


class TestForkFanoutExample(Base):
    def test_explore_once_then_fork_reviewers_and_verifiers(self):
        from test_examples import marker, nested_marker
        verdict = marker({"verdicts": [{"id": "c-1", "refuted": False, "reasoning": "reachable"},
                                       {"id": "c-2", "refuted": True, "reasoning": "unreachable"}]})
        dims = [{"key": "correctness", "focus": nested_marker({"findings": [
                    {"id": "c-1", "title": "off by one", "file": "a.py", "line": 3, "claim": "loop skips last " + verdict},
                    {"id": "c-2", "title": "bogus", "file": "a.py", "line": 9, "claim": "never happens"}]})},
                {"key": "security", "focus": marker({"findings": []})}]
        example = pathlib.Path(SCRIPT).parent.parent / "examples" / "fork-fanout.py"
        proc = self.h.run("run", str(example), "--args", json.dumps(
            {"root": self.h.tmp, "target": "the diff", "dimensions": dims}))
        res = self.result(self.h.run_id_from(proc))
        self.assertEqual([f["id"] for f in res["confirmed"]], ["c-1"])
        by_dim = {d["dimension"]: d for d in res["dimensions"]}
        self.assertEqual(by_dim["correctness"]["dropped"], ["c-2"])
        c = calls(self.h)
        explorer = c[0]
        self.assertFalse(explorer["fork"])
        forks = [x for x in c if x["fork"]]
        self.assertEqual(len(forks), 3, "two reviewers and one verifier fork the explorer")
        self.assertTrue(all(x["fork"] == "sess-1" for x in forks))
        self.assertTrue(all(dirs(self.h)[x["n"]] == self.h.tmp for x in forks))
        self.assertEqual(len(c), 5, "explorer + 2 reviews + 1 verify + synthesis")


if __name__ == "__main__":
    unittest.main()
