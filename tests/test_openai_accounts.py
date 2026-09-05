import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_headless_workflow import Harness, load_module


class OpenAIAccountsTest(unittest.TestCase):
    def test_named_account_dispatch_and_fork_keep_selection(self):
        h = Harness()
        script = h.write_script('''
            async def main(wf, args):
                parent = await wf.agent('hi', route='sol', openai_account='aether')
                child = await wf.fork(parent, 'hello')
                return [parent.to_dict(), child.to_dict()]
        ''')
        run = h.run('run', script)
        results = h.result(h.run_id_from(run))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertTrue(results[1]['forked'])
        for result in results:
            self.assertEqual(result['openai_account'], 'aether')
            self.assertEqual((Path(result['run_dir']) / 'openai_account').read_text().strip(), 'aether')

    def test_named_route_account_change_invalidates_journal(self):
        h = Harness()
        routes = Path(h.tmp) / 'routes.json'
        script = h.write_script("async def main(wf, args):\n    return (await wf.agent('hello', route='sol')).to_dict()")
        routes.write_text(json.dumps({'sol': {'openai_account': 'default'}}))
        run_id = h.run_id_from(h.run('run', script, '--routes', str(routes)))
        routes.write_text(json.dumps({'sol': {'openai_account': 'aether'}}))
        h.run('run', script, '--routes', str(routes), '--resume', run_id)
        self.assertEqual(len(h.calls()), 2)
        self.assertEqual(h.result(run_id)['openai_account'], 'aether')

    def test_preflight_is_scoped_to_account_and_blocks_unknown(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as root:
            wf = mod.Workflow('test', root, mod.DEFAULT_ROUTES, '/unused', 10, 1, True, True, None)
            default = dict(mod.DEFAULT_ROUTES['sol'], openai_account='default')
            aether = dict(default, openai_account='aether')
            with mock.patch.object(mod, 'find_quota_script', return_value='/quota.py'), \
                 mock.patch.object(mod.subprocess, 'run', side_effect=[mock.Mock(returncode=20), mock.Mock(returncode=0)]) as run:
                self.assertFalse(wf._quota_ok(default))
                self.assertTrue(wf._quota_ok(aether))
                self.assertFalse(wf._quota_ok(default))
            self.assertEqual(run.call_count, 2)
            self.assertIn('aether', run.call_args.args[0])
            wf.quota_cache.clear()
            with mock.patch.object(mod, 'find_quota_script', return_value='/quota.py'), \
                 mock.patch.object(mod.subprocess, 'run', return_value=mock.Mock(returncode=21)):
                self.assertFalse(wf._quota_ok(aether))


if __name__ == '__main__':
    unittest.main()
