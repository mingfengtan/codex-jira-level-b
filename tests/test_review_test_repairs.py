"""Regression tests for review-fix verification and bounded automatic repairs."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import review_worker as rev
from test_review_loop import MockCfg


class FixVerificationRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = MockCfg(self.root)
        self.sha = 'a' * 40
        self.info = {'state': 'open', 'merged_at': None,
                     'head': {'sha': self.sha, 'ref': 'codex/SSG-4', 'repo': {'full_name': 'a/b'}},
                     'base': {'ref': 'main'}, 'changed_files': 1}
        job = {'key': 'SSG-4', 'branch': 'codex/SSG-4',
               'pr_url': 'https://github.com/a/b/pull/12'}
        self.s = rev.create_state(self.cfg, job)
        self.s.update(stage='fix_verify', reviewed_sha=self.sha,
                      fix_session='saved-codex-session', fix_rounds=1)
        self.fix_dir = rev.review_dir(self.cfg) / 'worktrees' / 'SSG-4-fix'
        self.fix_dir.mkdir(parents=True, exist_ok=True)
        self.env = patch.dict(os.environ, {'MAX_REVIEW_TEST_REPAIRS':'2'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def perform_with_checks(self, code, out):
        with patch.object(rev, 'repository_name', return_value='a/b'), \
             patch.object(rev, 'pr_info', return_value=self.info), \
             patch.object(rev, 'verify_fix', return_value=(code, out, self.root/'check.log')):
            rev.perform(self.cfg, self.s)

    def test_code_error_queues_agent_repair_without_pushing(self):
        self.perform_with_checks(1, 'Some tests failed: Actual <-1.0>, Expected >= 0')
        self.assertEqual(self.s['stage'], 'fix_test_repair')
        self.assertEqual(self.s['test_repair_attempts'], 1)
        self.assertEqual(self.s['fix_rounds'], 1)
        self.assertEqual(self.s['fix_session'], 'saved-codex-session')
        self.assertFalse(self.s['fix_completed'])
        self.assertIn('Some tests failed', self.s['test_failure_summary'])

    def test_next_tick_reuses_saved_agent_session_and_returns_to_verification(self):
        self.s.update(stage='fix_test_repair', test_repair_attempts=1,
                      test_failure_summary='Some tests failed', fix_completed=False)
        sessions=[]
        def agent(cfg, state, path, prompt, role, output, schema=None):
            sessions.append(state['fix_session'])
            self.assertEqual(role, 'fix')
            self.assertIn('Some tests failed', prompt)
            output.write_text('Repaired')
            state['fix_completed']=True
        with patch.object(rev, 'repository_name', return_value='a/b'), \
             patch.object(rev, 'pr_info', return_value=self.info), \
             patch.object(rev, 'agent_run', side_effect=agent):
            rev.perform(self.cfg, self.s)
        self.assertEqual(sessions, ['saved-codex-session'])
        self.assertEqual(self.s['stage'], 'fix_verify')

    def test_crash_after_completed_repair_does_not_start_agent_again(self):
        self.s.update(stage='fix_test_repair', test_repair_attempts=1,
                      test_failure_summary='Some tests failed', fix_completed=True)
        logfile=rev.review_dir(self.cfg) / 'logs'
        logfile.mkdir(exist_ok=True)
        (logfile / 'SSG-4.fix.repair.1.last.md').write_text('Done')
        with patch.object(rev, 'repository_name', return_value='a/b'), \
             patch.object(rev, 'pr_info', return_value=self.info), \
             patch.object(rev, 'agent_run') as called:
            rev.perform(self.cfg, self.s)
        called.assert_not_called()
        self.assertEqual(self.s['stage'], 'fix_verify')

    def test_only_two_repair_attempts_then_manual(self):
        self.s['test_repair_attempts']=2
        self.perform_with_checks(1, 'Some tests failed. Actual: -1.0')
        self.assertEqual(self.s['stage'], 'manual')
        self.assertIn('after 2 automatic repairs', self.s['error'])

    def test_environment_failure_is_not_sent_to_agent(self):
        self.perform_with_checks(1, '/bin/bash: flutter: command not found')
        self.assertEqual(self.s['stage'], 'manual')
        self.assertEqual(self.s.get('test_repair_attempts', 0), 0)

    def test_unknown_failure_is_manual_instead_of_spending_quota(self):
        self.perform_with_checks(1, 'process exited with unknown reason')
        self.assertEqual(self.s['stage'], 'manual')

    def test_a_successful_verification_commits_and_pushes(self):
        calls=[]
        def git(args, **kwargs):
            calls.append(args)
            if 'diff' in args:
                return 'lib/physics.dart'
            if 'rev-parse' in args:
                return 'b' * 40
            return ''
        with patch.object(rev, 'repository_name', return_value='a/b'), \
             patch.object(rev, 'pr_info', return_value=self.info), \
             patch.object(rev, 'verify_fix', return_value=(0, 'All tests passed', self.root/'check.log')), \
             patch.object(rev.core, 'run_command', side_effect=git):
            rev.perform(self.cfg, self.s)
        self.assertEqual(self.s['stage'], 'recheck')
        self.assertTrue(any('commit' in args for args in calls))
        self.assertTrue(any('push' in args for args in calls))

    def test_real_log_capture_for_nonzero_exit(self):
        self.cfg.check_command='printf "Some tests failed\\n"; exit 1'
        code, tail, path=rev.verify_fix(self.cfg, self.s, self.fix_dir)
        self.assertEqual(code,1)
        self.assertIn('Some tests failed',tail)
        self.assertEqual(path.read_text(), 'Some tests failed\n')

    def test_classifier_distinguishes_failure_types(self):
        self.assertEqual(rev.verification_failure_kind('Some tests failed'), 'code')
        self.assertEqual(rev.verification_failure_kind('No module named pytest'), 'environment')
        self.assertEqual(rev.verification_failure_kind('flutter: command not found'), 'environment')
        self.assertEqual(rev.verification_failure_kind('unknown exit'), 'unknown')

if __name__ == '__main__':
    unittest.main()
