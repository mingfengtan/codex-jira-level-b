import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import (Config, classify_failure, load_job, new_job, persist_job, pause_due,
                          once, commit_if_needed, existing_pr, run_saved_job, validate_plan)


class FakeJira:
    active = None
    def __init__(self, cfg):
        self.cfg = cfg
        self.data = self.active
    def ready_issues(self):
        return [{'key': key} for key, issue in self.data.items() if issue['fields']['status']['name'] == 'Ready']
    def issue(self, key):
        return self.data[key]
    def dependencies_satisfied(self, issue):
        return True
    def transition(self, key, target):
        self.data[key]['fields']['status']['name'] = target
    def comment(self, key, message):
        self.data[key].setdefault('comments', []).append(message)
    def has_comment_marker(self, key, marker):
        return any(marker in comment for comment in self.data[key].get('comments', []))


def cli(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


class TestRecoveryIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'game'
        self.remote = self.root / 'remote.git'
        cli('git', 'init', '--bare', str(self.remote))
        cli('git', 'init', '-b', 'main', str(self.repo))
        cli('git', '-C', str(self.repo), 'config', 'user.name', 'Test Runner')
        cli('git', '-C', str(self.repo), 'config', 'user.email', 'test@example.com')
        (self.repo / 'README.md').write_text('Start\n')
        cli('git', '-C', str(self.repo), 'add', '-A')
        cli('git', '-C', str(self.repo), 'commit', '-m', 'Initial commit')
        cli('git', '-C', str(self.repo), 'remote', 'add', 'origin', str(self.remote))
        cli('git', '-C', str(self.repo), 'push', 'origin', 'main')
        self.worktrees = self.root / 'worktrees'
        self.cfg = Config(url='https://example.atlassian.net', email='e@example.com', token='secret',
                          project='AIDEV', repo=self.repo, base_branch='main', ready_status='Ready',
                          in_progress_status='In Progress', review_status='In Review', blocked_status='Blocked',
                          ready_label='codex-ready', check_command='python3 -c "from pathlib import Path; assert Path(\'new.py\').exists()"',
                          worktree_root=self.worktrees, codex_timeout=5, test_timeout=5,
                          quota_retry_seconds=3600, retry_base_seconds=1, max_retries=3, max_test_repairs=1)
        self.data = {'AIDEV-12': {'key': 'AIDEV-12', 'fields': {
            'summary': 'Create code', 'description': 'Create new.py',
            'status': {'name': 'Ready'}, 'issuetype': {'name': 'Task'},
            'labels': ['codex-ready'], 'subtasks': []}}}
        FakeJira.active = self.data
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.mode = self.root / 'mode'
        self.mode.write_text('success')
        self.logargs = self.root / 'args.log'
        self.prstore = self.root / 'prs.json'
        self.prstore.write_text('[]')
        codex_script = self.bin / 'codex'
        codex_script.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
with open(os.environ['TEST_ARGS_LOG'], 'a') as f: f.write(json.dumps(args)+'\\n')
mode=pathlib.Path(os.environ['TEST_MODE']).read_text().strip()
print(json.dumps({'type':'thread.started','thread_id':'0199a213-81c0-7800-8aa1-bbab2a035a53'}), flush=True)
if mode == 'quota':
 print(json.dumps({'type':'turn.failed','error':{'message':"You've hit your usage limit. Try later."}}), flush=True)
 sys.exit(1)
if mode == 'error':
 print(json.dumps({'type':'turn.failed','error':{'message':'temporary connection reset'}}), flush=True)
 sys.exit(1)
pathlib.Path('new.py').write_text('value = 456\\n' if mode == 'fix' or (mode == 'auto-fix' and 'resume' in args) else 'value = 123\\n')
if '--output-last-message' in args:
 pathlib.Path(args[args.index('--output-last-message')+1]).write_text('Implemented safely')
print(json.dumps({'type':'turn.completed'}), flush=True)
''')
        codex_script.chmod(0o755)
        gh_script = self.bin / 'gh'
        gh_script.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
store=pathlib.Path(os.environ['TEST_PR_STORE'])
prs=json.loads(store.read_text())
if args[0:2] == ['pr','list']:
 print(json.dumps(prs)); sys.exit(0)
if args[0:2] == ['pr','create']:
 if prs: sys.exit('duplicate PR attempted')
 url='https://github.com/example/test/pull/1'
 prs.append({'url':url,'state':'OPEN'})
 store.write_text(json.dumps(prs))
 print(url); sys.exit(0)
sys.exit('unknown gh args '+str(args))
''')
        gh_script.chmod(0o755)
        self.patch = patch.dict(os.environ, {'PATH': f'{self.bin}:{os.environ["PATH"]}',
                                            'TEST_MODE': str(self.mode),
                                            'TEST_ARGS_LOG': str(self.logargs),
                                            'TEST_PR_STORE': str(self.prstore)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.jira_patch = patch('orchestrator.Jira', FakeJira)
        self.jira_patch.start()
        self.addCleanup(self.jira_patch.stop)

    def test_complete_and_no_duplicate_pr_after_second_tick(self):
        once(self.cfg)
        job = load_job(self.cfg, 'AIDEV-12')
        self.assertEqual(job['stage'], 'complete')
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'In Review')
        self.assertEqual(len(self.data['AIDEV-12']['comments']), 1)
        self.assertEqual(len(json.loads(self.prstore.read_text())), 1)
        self.assertFalse(Path(job['worktree']).exists())
        once(self.cfg)
        self.assertEqual(len(json.loads(self.prstore.read_text())), 1)
        self.assertIn('AIDEV-12:', cli('git', '--git-dir', str(self.remote), 'log',
                                      'refs/heads/codex/AIDEV-12', '--format=%s'))

    def test_quota_pause_then_session_specific_resume(self):
        self.mode.write_text('quota')
        once(self.cfg)
        job = load_job(self.cfg, 'AIDEV-12')
        self.assertEqual(job['stage'], 'codex')
        self.assertEqual(job['failure_kind'], 'quota')
        self.assertEqual(job['session_id'], '0199a213-81c0-7800-8aa1-bbab2a035a53')
        self.assertTrue(Path(job['worktree']).exists())
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'In Progress')
        self.assertEqual(len(self.logargs.read_text().splitlines()), 1)
        once(self.cfg)  # The cooldown prevents another request.
        self.assertEqual(len(self.logargs.read_text().splitlines()), 1)
        self.mode.write_text('success')
        job['next_retry_at'] = '2000-01-01T00:00:00+00:00'
        persist_job(self.cfg, job)
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')
        calls = [json.loads(line) for line in self.logargs.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn('resume', calls[1])
        self.assertIn(job['session_id'], calls[1])

    def test_crash_after_pr_creation_reuses_existing_pr(self):
        # Simulate a PR being created, then the controller dying before persisting its URL.
        cli('git', '-C', str(self.repo), 'fetch', 'origin', 'main')
        tree = self.worktrees / 'AIDEV-12'
        self.worktrees.mkdir()
        cli('git', '-C', str(self.repo), 'worktree', 'add', '-b', 'codex/AIDEV-12',
            str(tree), 'origin/main')
        (tree / 'new.py').write_text('value=123\n')
        cli('git', '-C', str(tree), 'add', '-A')
        cli('git', '-C', str(tree), 'commit', '-m', 'AIDEV-12: Create code')
        cli('git', '-C', str(tree), 'push', '-u', 'origin', 'codex/AIDEV-12')
        self.prstore.write_text(json.dumps([{'url':'https://github.com/example/test/pull/1', 'state':'OPEN'}]))
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job['stage'] = 'pr'
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['status']['name'] = 'In Progress'
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')
        self.assertEqual(len(self.data['AIDEV-12']['comments']), 1)
        self.assertEqual(len(json.loads(self.prstore.read_text())), 1)

    def test_crash_after_jira_comment_does_not_repeat_it(self):
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job['stage'] = 'jira'
        job['pr_url'] = 'https://github.com/example/test/pull/1'
        marker = 'codex-jira:AIDEV-12:pr'
        self.data['AIDEV-12']['comments'] = ['['+marker+'] Already added']
        self.data['AIDEV-12']['fields']['status']['name'] = 'In Progress'
        persist_job(self.cfg, job)
        # No worktree exists; cleanup is idempotent.
        once(self.cfg)
        self.assertEqual(len(self.data['AIDEV-12']['comments']), 1)
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'In Review')

    def test_authorization_removed_stops_worker_without_deleting_changes(self):
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job['stage'] = 'codex'
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['labels'] = []
        self.data['AIDEV-12']['fields']['status']['name'] = 'In Progress'
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'manual')

    def test_test_failures_send_codex_back_to_repair_in_same_invocation(self):
        from dataclasses import replace
        cfg = replace(self.cfg, check_command='python3 -c "from pathlib import Path; assert \'456\' in Path(\'new.py\').read_text()"')
        self.mode.write_text('auto-fix')
        once(cfg)
        job = load_job(cfg, 'AIDEV-12')
        self.assertEqual(job['stage'], 'complete')
        self.assertEqual(job['verify_repairs'], 1)
        self.assertIsNone(job['repair_context'])
        self.assertIsNone(job['next_retry_at'])
        calls = [json.loads(line) for line in self.logargs.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn('resume', calls[1])
        self.assertTrue(any('latest verification output' in arg for arg in calls[1]))
        self.assertEqual(len(json.loads(self.prstore.read_text())), 1)

    def test_repeated_failing_verifications_stop_after_budget_without_pushing(self):
        from dataclasses import replace
        cfg = replace(self.cfg, max_test_repairs=2,
                      check_command='python3 -c "from pathlib import Path; assert \'456\' in Path(\'new.py\').read_text()"')
        once(cfg)
        job = load_job(cfg, 'AIDEV-12')
        self.assertEqual(job['stage'], 'manual')
        self.assertEqual(job['verify_repairs'], 3)
        self.assertEqual(job['failure_kind'], 'manual')
        self.assertEqual(job['resume_stage'], 'codex')
        self.assertIn('after 2 Codex repair attempts', job['last_error'])
        self.assertEqual(len(self.logargs.read_text().splitlines()), 3)
        self.assertEqual(json.loads(self.prstore.read_text()), [])
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'Blocked')

    def test_old_transient_verification_pause_auto_resumes_without_cooldown(self):
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job.update(stage='codex', failure_kind='transient',
                   repair_context='ValueError: expected 456 but got 123',
                   last_error='Verification failed; Codex will attempt repair. See /test.log',
                   next_retry_at='2099-01-01T00:00:00+00:00', verify_repairs=1)
        persist_job(self.cfg, job)
        self.mode.write_text('fix')
        once(self.cfg)
        result = load_job(self.cfg, 'AIDEV-12')
        self.assertEqual(result['stage'], 'complete')
        self.assertIsNone(result['next_retry_at'])
        self.assertEqual(result['verify_repairs'], 1)

    def test_budget_raise_unblocks_only_verification_manual_stop(self):
        from dataclasses import replace
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job.update(stage='manual', resume_stage='codex', failure_kind='manual',
                   repair_context='Expected 456; got 123', verify_repairs=3,
                   last_error='Verification failed 3 times; inspect /test.log')
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['status']['name'] = 'Blocked'
        self.mode.write_text('fix')
        cfg = replace(self.cfg, max_test_repairs=4)
        once(cfg)
        result = load_job(cfg, 'AIDEV-12')
        self.assertEqual(result['stage'], 'complete')
        self.assertEqual(result['verify_repairs'], 3)
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'In Review')

    def test_old_manual_from_transient_verification_retries_auto_resumes(self):
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job.update(stage='manual', resume_stage='codex', failure_kind='transient',
                   repair_context='Flutter test failed', verify_repairs=1,
                   last_error='Verification failed; Codex will attempt repair. See /test.log')
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['status']['name'] = 'Blocked'
        self.mode.write_text('fix')
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'In Review')

    def test_other_manual_failures_remain_blocked_after_budget_raise(self):
        from dataclasses import replace
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job.update(stage='manual', resume_stage='codex', failure_kind='manual',
                   repair_context='irrelevant', verify_repairs=3,
                   last_error='Authorization label removed')
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['status']['name'] = 'Blocked'
        once(replace(self.cfg, max_test_repairs=4))
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'manual')
        self.assertFalse(self.logargs.exists())

    def test_unexpected_cli_error_is_bounded_and_recoverable(self):
        self.mode.write_text('error')
        once(self.cfg)
        job = load_job(self.cfg, 'AIDEV-12')
        self.assertEqual(job['failure_kind'], 'transient')
        job['next_retry_at'] = '2000-01-01T00:00:00+00:00'
        persist_job(self.cfg, job)
        self.mode.write_text('success')
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')

    def test_interrupt_preserves_partial_job_and_resume(self):
        from orchestrator import run_codex_stream
        def abrupt(*args, **kwargs):
            (self.worktrees / 'AIDEV-12' / 'partial.txt').write_text('partial')
            raise KeyboardInterrupt()
        with patch('orchestrator.run_codex_stream', side_effect=abrupt):
            with self.assertRaises(KeyboardInterrupt):
                once(self.cfg)
        job = load_job(self.cfg, 'AIDEV-12')
        self.assertEqual(job['stage'], 'codex')
        self.assertTrue((self.worktrees / 'AIDEV-12' / 'partial.txt').exists())
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')

    def test_completed_codex_checkpoint_skips_duplicate_run(self):
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job['stage'] = 'codex'
        job['codex_complete'] = True
        self.worktrees.mkdir(exist_ok=True)
        cli('git', '-C', str(self.repo), 'fetch', 'origin', 'main')
        cli('git', '-C', str(self.repo), 'worktree', 'add', '-b', 'codex/AIDEV-12',
            job['worktree'], 'origin/main')
        (Path(job['worktree']) / 'new.py').write_text('value=123\n')
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['status']['name'] = 'In Progress'
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')
        self.assertFalse(self.logargs.exists())

    def test_saved_active_process_group_blocks_duplicate_codex(self):
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job['stage'] = 'codex'
        job['active_pid'] = os.getpgrp()
        job['active_kind'] = 'codex'
        self.data['AIDEV-12']['fields']['status']['name'] = 'In Progress'
        persist_job(self.cfg, job)
        once(self.cfg)
        job = load_job(self.cfg, 'AIDEV-12')
        self.assertEqual(job['failure_kind'], 'transient')
        self.assertIn('Previous codex process group', job['last_error'])
        self.assertFalse(self.logargs.exists())

    def test_repeated_nonquota_failures_stop_for_review(self):
        self.mode.write_text('error')
        for attempt in range(3):
            once(self.cfg)
            job = load_job(self.cfg, 'AIDEV-12')
            if attempt < 2:
                self.assertEqual(job['stage'], 'codex')
                job['next_retry_at'] = '2000-01-01T00:00:00+00:00'
                persist_job(self.cfg, job)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'manual')
        self.assertEqual(self.data['AIDEV-12']['fields']['status']['name'], 'Blocked')
        self.assertEqual(len(self.data['AIDEV-12']['comments']), 1)

    def test_restart_at_commit_does_not_rerun_codex(self):
        # Checkout and completed code existed before controller crash.
        job = new_job(self.cfg, self.data['AIDEV-12'])
        job['stage'] = 'commit'
        self.worktrees.mkdir(exist_ok=True)
        cli('git', '-C', str(self.repo), 'fetch', 'origin', 'main')
        cli('git', '-C', str(self.repo), 'worktree', 'add', '-b', 'codex/AIDEV-12',
            job['worktree'], 'origin/main')
        (Path(job['worktree']) / 'new.py').write_text('value=123\n')
        persist_job(self.cfg, job)
        self.data['AIDEV-12']['fields']['status']['name'] = 'In Progress'
        once(self.cfg)
        self.assertEqual(load_job(self.cfg, 'AIDEV-12')['stage'], 'complete')
        self.assertFalse(self.logargs.exists())


class TestClassifier(unittest.TestCase):
    def test_quota_and_nonquota(self):
        self.assertEqual(classify_failure("You've hit your usage limit"), 'quota')
        self.assertEqual(classify_failure('rate limit exceeded'), 'quota')
        self.assertEqual(classify_failure('Internal service unavailable'), 'transient')
        self.assertEqual(classify_failure('permission denied'), 'manual')


if __name__ == '__main__':
    unittest.main()
