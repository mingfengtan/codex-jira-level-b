"""Offline regression tests for continuous review-run and explicit human recovery."""
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from datetime import timedelta
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestrator as core
import review_worker as rev
from test_review_loop import MockCfg


class ReviewRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = MockCfg(self.root)
        self.sha = 'a' * 40
        self.other_sha = 'b' * 40
        self.job = {'key':'SSG-8','stage':'complete', 'pr_url':'https://github.com/a/b/pull/12',
                    'branch':'codex/SSG-8'}
        self.s = rev.create_state(self.cfg, self.job)
        self.info = {'state':'open','merged_at':None,
                     'head':{'sha':self.sha, 'ref':'codex/SSG-8', 'repo':{'full_name':'a/b'}},
                     'base':{'ref':'main'}, 'changed_files':1}
        self.enabled = patch.dict(os.environ, {'AUTO_REVIEW_ENABLED':'true',
                                                'MAX_AUTO_REVIEW_FIXES':'2',
                                                'MAX_REVIEW_TEST_REPAIRS':'2'})
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def run_mocked(self, *, perform, issue_key='SSG-8', max_minutes=1):
        class Jira:
            def __init__(self,cfg): pass
            def issue(self,key): return {'fields':{'status':{'name':'In Review'}}}
        with patch.object(core,'single_worker_lock',return_value=nullcontext()), \
             patch.object(core,'all_jobs',return_value=[self.job]), \
             patch.object(core,'Jira',Jira), \
             patch.object(rev,'load_state',return_value=self.s), \
             patch.object(rev,'perform',side_effect=perform) as call:
            rev.review_run(self.cfg,issue_key=issue_key,max_minutes=max_minutes)
            return call

    def test_immediate_check_when_existing_head_wait_is_already_satisfied(self):
        self.s.update(stage='recheck', error='Waiting for GitHub PR head to reflect the pushed fix',
                      next_retry_at=(core.utcnow() + timedelta(hours=1)).isoformat())
        def finished(cfg, state, **kw):
            self.assertTrue(kw['auto_advance'])
            rev.stage_to(cfg,state,'passed')
        with patch.object(rev.time,'sleep') as sleep:
            called = self.run_mocked(perform=finished)
        self.assertEqual(called.call_count,1)
        sleep.assert_not_called()
        self.assertEqual(self.s['stage'],'passed')

    def test_immediate_check_when_ci_has_already_passed(self):
        self.s.update(stage='ci', error='Waiting for GitHub CI checks',
                      next_retry_at=(core.utcnow() + timedelta(hours=1)).isoformat())
        with patch.object(rev.time,'sleep') as sleep:
            called = self.run_mocked(perform=lambda cfg,s,**kw: rev.stage_to(cfg,s,'passed'))
        self.assertEqual(called.call_count,1)
        sleep.assert_not_called()

    def test_passed_checkpoint_is_invalidated_if_new_commit_is_pushed(self):
        self.s.update(stage='passed', reviewed_sha=self.sha,review_rounds=2,
                      result={'verdict':'pass'}, review_completed=True)
        self.info['head']['sha'] = self.other_sha
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info):
            call = self.run_mocked(perform=lambda cfg,state,**kw: rev.stage_to(cfg,state,'passed'))
        self.assertEqual(call.call_count,1)
        self.assertEqual(self.s['review_rounds'],2)
        self.assertIsNone(self.s['reviewed_sha'])
        self.assertIsNone(self.s['result'])

    def test_stops_when_manual_intervention_is_already_required(self):
        self.s.update(stage='manual', error='Automatic review fix budget exhausted',fix_rounds=2)
        with self.assertRaisesRegex(core.WorkerError,'manual intervention required'):
            self.run_mocked(perform=lambda *a,**kw: self.fail('must not start Agent A or B'))
        self.assertEqual(self.s['stage'],'manual')
        self.assertEqual(self.s['fix_rounds'],2)

    def test_stops_when_budget_is_exhausted_during_review(self):
        self.s.update(stage='publish', reviewed_sha=self.sha,fix_rounds=2,
                      result={'verdict':'changes_requested','summary':'issue remains','findings':[]})
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info), \
             patch.object(rev,'publish_review'), \
             patch.object(rev,'agent_run') as agent:
            with self.assertRaisesRegex(core.WorkerError,'budget exhausted'):
                self.run_mocked(perform=rev.perform)
        agent.assert_not_called()
        self.assertEqual(self.s['stage'],'manual')
        self.assertEqual(self.s['fix_rounds'],2)

    def test_failed_verification_repairs_before_any_push_in_auto_advance(self):
        """Regression: failing verification must NOT accidentally stage fix_commit."""
        self.s.update(stage='fix_verify',reviewed_sha=self.sha,fix_rounds=1,
                      fix_session='existing-thread')
        fix_dir = rev.review_dir(self.cfg) / 'worktrees' / 'SSG-8-fix'
        fix_dir.mkdir(parents=True)
        calls=[]
        def git(argv,**kw):
            calls.append(argv)
            if 'rev-parse' in argv: return self.other_sha
            if 'diff' in argv: return 'lib/physics.dart'
            return ''
        def verify(cfg,state,path):
            number = sum(1 for c in calls if c == 'verified')
            calls.append('verified')
            if number == 0:
                return (1,'Some tests failed: static collision Actual: -1.0',self.root/'verify.log')
            return (0,'All tests passed',self.root/'verify.log')
        def agent(cfg,state,path,prompt,role,out,schema=None):
            self.assertEqual(state['fix_session'],'existing-thread')
            self.assertIn('Some tests failed',prompt)
            calls.append('agent-repair')
            out.write_text('Repaired')
            state['fix_completed']=True
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info), \
             patch.object(rev,'verify_fix',side_effect=verify), \
             patch.object(rev,'agent_run',side_effect=agent), \
             patch.object(core,'run_command',side_effect=git):
            rev.perform(self.cfg,self.s,auto_advance=True)
        self.assertEqual(self.s['stage'],'recheck')
        self.assertEqual(self.s['test_repair_attempts'],1)
        self.assertLess(calls.index('agent-repair'),next(i for i,c in enumerate(calls) if isinstance(c,list) and 'commit' in c))
        self.assertEqual(len([c for c in calls if isinstance(c,list) and 'push' in c]),1)

    def test_head_polling_stops_at_configured_limit_without_resetting_checkpoint(self):
        self.s.update(stage='recheck',error='Waiting for GitHub PR head to reflect the pushed fix',
                      next_retry_at=(core.utcnow() + timedelta(hours=1)).isoformat())
        class Clock:
            now = 0.0
            def monotonic(self): return self.now
            def sleep(self, seconds): self.now += seconds
        clock = Clock()
        real_config = rev.config
        def cfg():
            settings = real_config()
            settings.update(head_wait_seconds=3,head_poll_seconds=1)
            return settings
        with patch.object(rev,'config',side_effect=cfg), \
             patch.object(rev.time,'monotonic',side_effect=clock.monotonic), \
             patch.object(rev.time,'sleep',side_effect=clock.sleep):
            call = self.run_mocked(perform=lambda cfg,state,**kw: None)
        self.assertGreaterEqual(call.call_count,2)
        self.assertLessEqual(call.call_count,4)
        self.assertGreaterEqual(clock.now,3)
        self.assertEqual(self.s['stage'],'recheck')
        self.assertEqual(self.s['fix_rounds'],0)

    def test_resume_requires_new_human_push_and_keeps_fix_budget(self):
        self.s.update(stage='manual', error='Automatic review fix budget exhausted',
                      reviewed_sha=self.sha,fix_rounds=2,review_rounds=3,
                      review_completed=True,result={'verdict':'changes_requested'})
        rev.persist(self.cfg,self.s)
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info):
            with self.assertRaisesRegex(ValueError,'PR HEAD has not changed'):
                rev.review_resume_after_human_push(self.cfg,'SSG-8')
        self.info['head']['sha'] = self.other_sha
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info):
            rev.review_resume_after_human_push(self.cfg,'SSG-8')
        saved=rev.load_state(self.cfg,'SSG-8')
        self.assertEqual(saved['stage'],'review')
        self.assertIsNone(saved['result'])
        self.assertEqual(saved['fix_rounds'],2)
        self.assertEqual(saved['review_rounds'],3)
        self.assertEqual(saved['human_resumed_to_sha'],self.other_sha)

    def budget_stopped_state(self):
        self.s.update(stage='manual', error='Automatic review fix budget exhausted',
                      failure_kind='manual', reviewed_sha=self.sha, fix_rounds=2,
                      review_rounds=3, result={
                          'verdict': 'changes_requested', 'summary': 'Fix collision response',
                          'findings': [{'id': '1', 'severity': 'high', 'path': 'lib/collision.dart',
                                        'line': 11, 'body': 'Incorrect normal'}]})
        rev.persist(self.cfg, self.s)

    def test_config_increase_resumes_existing_review_without_resetting_counts(self):
        self.budget_stopped_state()
        seen = []
        def finish(cfg, state, **kwargs):
            seen.append((state['stage'], state['fix_rounds'], state['review_rounds'],
                         state['reviewed_sha'], state['result']['summary']))
            rev.stage_to(cfg, state, 'passed')
        with patch.dict(os.environ, {'MAX_AUTO_REVIEW_FIXES': '4'}):
            called = self.run_mocked(perform=finish)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(seen, [('fix_prepare', 2, 3, self.sha, 'Fix collision response')])
        saved = rev.load_state(self.cfg, 'SSG-8')
        self.assertEqual(saved['stage'], 'passed')
        self.assertEqual(saved['fix_rounds'], 2)
        self.assertEqual(saved['review_rounds'], 3)

    def test_config_increase_dry_run_does_not_change_manual_checkpoint(self):
        self.budget_stopped_state()
        output = StringIO()
        with patch.dict(os.environ, {'MAX_AUTO_REVIEW_FIXES': '3'}), \
             patch.object(core, 'single_worker_lock', return_value=nullcontext()), \
             patch.object(core, 'all_jobs', return_value=[self.job]), \
             patch.object(core, 'Jira') as Jira, \
             patch.object(rev, 'perform') as perform, \
             redirect_stdout(output):
            Jira.return_value.issue.return_value = {'fields': {'status': {'name': 'In Review'}}}
            rev.review_run(self.cfg, issue_key='SSG-8', dry_run=True)
        self.assertIn('fixes used=2, new limit=3', output.getvalue())
        perform.assert_not_called()
        self.assertEqual(rev.load_state(self.cfg, 'SSG-8')['stage'], 'manual')

    def test_config_increase_resumes_using_legacy_review_once(self):
        self.budget_stopped_state()
        class Jira:
            def __init__(self, cfg): pass
            def issue(self, key): return {'fields': {'status': {'name': 'In Review'}}}
        seen = []
        with patch.dict(os.environ, {'MAX_AUTO_REVIEW_FIXES': '3'}), \
             patch.object(core, 'single_worker_lock', return_value=nullcontext()), \
             patch.object(core, 'all_jobs', return_value=[self.job]), \
             patch.object(core, 'Jira', Jira), \
             patch.object(rev, 'perform', side_effect=lambda cfg, state: seen.append(state['stage'])):
            rev.review_once(self.cfg)
        self.assertEqual(seen, ['fix_prepare'])
        self.assertEqual(rev.load_state(self.cfg, 'SSG-8')['stage'], 'fix_prepare')

    def test_config_increase_does_not_resume_other_manual_errors(self):
        self.budget_stopped_state()
        self.s['error'] = 'GitHub CI checks failed; inspect PR'
        rev.persist(self.cfg, self.s)
        with patch.dict(os.environ, {'MAX_AUTO_REVIEW_FIXES': '9'}):
            with self.assertRaisesRegex(core.WorkerError, 'manual intervention required'):
                self.run_mocked(perform=lambda *args, **kw: self.fail('must not start'))
        self.assertEqual(rev.load_state(self.cfg, 'SSG-8')['stage'], 'manual')

    def test_config_increase_requires_original_review_evidence(self):
        self.budget_stopped_state()
        with patch.dict(os.environ, {'MAX_AUTO_REVIEW_FIXES': '4'}):
            self.s['result'] = None
            self.assertFalse(rev.can_resume_with_raised_fix_budget(self.s, rev.config()['max_fixes']))
            self.s['result'] = {'verdict': 'pass'}
            self.assertFalse(rev.can_resume_with_raised_fix_budget(self.s, rev.config()['max_fixes']))
            self.s['result'] = {'verdict': 'changes_requested'}
            self.s['reviewed_sha'] = None
            self.assertFalse(rev.can_resume_with_raised_fix_budget(self.s, rev.config()['max_fixes']))

    def test_resume_rejects_other_manual_errors(self):
        self.s.update(stage='manual',error='CI checks failed',reviewed_sha=self.sha)
        rev.persist(self.cfg,self.s)
        with self.assertRaisesRegex(ValueError,'budget'):
            rev.review_resume_after_human_push(self.cfg,'SSG-8')


if __name__=='__main__':
    unittest.main()
