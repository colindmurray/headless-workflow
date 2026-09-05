from pathlib import Path
import unittest

from test_headless_workflow import Harness, load_module


class AstraRouteTest(unittest.TestCase):
    def test_route_has_bounded_defaults_and_is_not_an_automatic_fallback(self):
        mod = load_module()
        astra = mod.DEFAULT_ROUTES["astra"]
        self.assertEqual(astra["model"], "gpt-6-astra")
        self.assertEqual(astra["effort"], "medium")
        self.assertEqual(astra["max_concurrency"], 1)
        self.assertEqual(astra["quota"], "openai")
        self.assertEqual(astra["format"], "json")
        self.assertEqual(astra["fallback"], [])
        self.assertNotIn("openai_account", astra)
        for route in mod.DEFAULT_ROUTES.values():
            self.assertNotIn("astra", route.get("fallback", []))

    def test_astra_dispatch_and_fork_preserve_explicit_account_and_effort(self):
        h = Harness()
        script = h.write_script('''
            async def main(wf, args):
                parent = await wf.agent('@@REPLY:astra@@', route='astra',
                                        openai_account='aether', effort='high')
                child = await wf.fork(parent, '@@REPLY:fork@@')
                return [parent.to_dict(), child.to_dict()]
        ''')
        run = h.run("run", script)
        results = h.result(h.run_id_from(run))
        self.assertEqual(len(h.calls()), 2)
        self.assertTrue(results[1]["forked"])
        for call, result in zip(h.calls(), results):
            self.assertTrue(result["ok"])
            self.assertEqual(call["model"], "gpt-6-astra")
            self.assertEqual(call["effort"], "high")
            self.assertEqual(result["effort"], "high")
            self.assertEqual(result["model"], "gpt-6-astra")
            self.assertEqual(result["openai_account"], "aether")
            self.assertEqual((Path(result["run_dir"]) / "format").read_text().strip(), "json")


if __name__ == "__main__":
    unittest.main()
