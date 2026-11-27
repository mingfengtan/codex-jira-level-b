"""Offline regression tests for the opt-in two-agent review worker and rich links."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestrator as core
import review_worker as rev


class MockCfg:
    def __init__(self, root):
        self.worktree_root = root / 'worktrees'
        self.repo = root / 'repo'
        self.repo.mkdir(parents=True, exist_ok=True)
        self.project = 'SSG'
        self.url = 'https://example.atlassian.net'
        self.base_branch = 'main'
        self.review_status = 'In Review'
        self.check_command = 'flutter test'
        self.test_timeout = 90
        self.codex_timeout = 90
        self.max_retries = 3
        self.quota_retry_seconds = 1800
        self.retry_base_seconds = 60


class LinksTests(unittest.TestCase):
    def test_jira_comment_has_clickable_pr_link_mark(self):
        from orchestrator import Jira
        jira = object.__new__(Jira)
        calls = []
        jira.call = lambda method, path, body: calls.append((method, path, body))
        jira.comment('SSG-4', '[codex-jira:SSG-4:pr]\nDraft PR: https://github.com/a/b/pull/12.')
        self.assertEqual(calls[0][1], '/issue/SSG-4/comment')
        body = calls[0][2]['body']
        p = body['content'][1]['content']
        link = next(node for node in p if node.get('marks'))
        self.assertEqual(link['text'], 'https://github.com/a/b/pull/12')
        self.assertEqual(link['marks'][0]['attrs']['href'], link['text'])
        self.assertIn('[codex-jira:SSG-4:pr]', core.plain(body))
        self.assertTrue(core.plain(body).endswith('.'))

    def test_pr_description_uses_full_jira_markdown_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = MockCfg(Path(tmp))
            job = {'key': 'SSG-4', 'branch': 'codex/SSG-4', 'worktree': str(cfg.repo)}
            out = cfg.repo / 'last.md'
            out.write_text('Added tests')
            calls = []
            def cmd(argv, **kwargs):
                calls.append(argv)
                return 'https://github.com/a/b/pull/12\n'
            with patch.object(core, 'existing_pr', return_value=None), patch.object(core, 'run_command', side_effect=cmd):
                self.assertEqual(core.ensure_pr(cfg, job, 'Set up CI', out), 'https://github.com/a/b/pull/12')
            pr_body = calls[-1][calls[-1].index('--body') + 1]
            self.assertIn('[SSG-4](https://example.atlassian.net/browse/SSG-4)', pr_body)
            self.assertIn('human review required', pr_body)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = MockCfg(self.root)
        self.sha = 'a' * 40
        self.info = {'state':'open', 'merged_at':None, 'head':{'sha':self.sha, 'ref':'codex/SSG-4', 'repo':{'full_name':'a/b'}},
                     'base':{'ref':'main'}, 'changed_files':1}
        self.job = {'key':'SSG-4', 'branch':'codex/SSG-4', 'pr_url':'https://github.com/a/b/pull/12'}
        self.s = rev.create_state(self.cfg, self.job)
        self.fake_path = self.root / 'checkout'
        self.fake_path.mkdir()

    def test_diff_line_mapping(self):
        patch_text = '@@ -1,3 +1,4 @@\n old\n-old2\n+new2\n+more\n end\n'
        self.assertEqual(rev.changed_lines(patch_text), {1,2,3,4})

    def test_publish_review_creates_inline_and_general_comments(self):
        self.s['reviewed_sha'] = self.sha
        self.s['result'] = {'verdict':'changes_requested','summary':'Found problems',
                            'findings':[{'id':'R1','severity':'high','path':'lib/physics.dart','line':8,'body':'Guard zero length.'},
                                        {'id':'R2','severity':'low','path':'test/other.dart','line':0,'body':'Consider naming.'}]}
        captured = []
        def fake_run(args, **kwargs):
            captured.append(json.loads(Path(args[args.index('--input')+1]).read_text()))
            return '{}'
        with patch.object(rev, 'existing_review', return_value=False), \
             patch.object(rev, 'files_for_inline', return_value={'lib/physics.dart':{8}}), \
             patch.object(core, 'run_command', side_effect=fake_run):
            rev.publish_review(self.cfg, self.s, 'a/b', self.info)
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(req['event'],'COMMENT')
        self.assertEqual(req['commit_id'], self.sha)
        self.assertEqual(req['comments'][0]['path'],'lib/physics.dart')
        self.assertEqual(len(req['comments']),1)
        self.assertIn(rev.review_marker('SSG-4',self.sha),req['body'])

    def test_self_review_422_falls_back_to_linked_pr_comment(self):
        self.s.update(reviewed_sha=self.sha, result={
            'verdict':'pass', 'summary':'No blocking findings', 'findings':[]})
        targets=[]
        def run(args, **kw):
            targets.append(args)
            if '/reviews' in args[4]:
                raise RuntimeError('HTTP 422: can not review own pull request')
            return '{}'
        with patch.object(rev, 'existing_review', return_value=False), \
             patch.object(rev, 'files_for_inline', return_value={}), \
             patch.object(rev, 'existing_pr_comment', return_value=False), \
             patch.object(core, 'run_command', side_effect=run):
            rev.publish_review(self.cfg,self.s,'a/b',self.info)
        self.assertEqual(len(targets),2)
        self.assertIn('/issues/12/comments', targets[1][4])

    def test_review_deduplication_after_crash(self):
        self.s.update(reviewed_sha=self.sha, result={'verdict':'pass','summary':'Looks good','findings':[]})
        with patch.object(rev, 'existing_review', return_value=True), \
             patch.object(core, 'run_command') as called:
            rev.publish_review(self.cfg, self.s, 'a/b', self.info)
            called.assert_not_called()

    def test_review_passes_after_ci_and_posts_jira_comment(self):
        self.s['review_running_sha']=None
        jira_comments=[]
        class FakeJira:
            def __init__(self, cfg): pass
            def has_comment_marker(self,key,marker): return False
            def comment(self,key,body): jira_comments.append((key,body))
        def agent(cfg, state, path, prompt, role, output, schema=None):
            self.assertEqual(role,'review')
            self.assertTrue(schema)
            output.write_text(json.dumps({'verdict':'pass','summary':'No issues','findings':[]}))
            state['review_completed']=True
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info), \
             patch.object(rev,'begin_worktree',return_value=self.fake_path), \
             patch.object(rev,'agent_run',side_effect=agent), \
             patch.object(rev,'publish_review') as published, \
             patch.object(rev,'pr_checks',return_value='passed'), \
             patch.object(rev,'forget_worktree'), \
             patch.object(core,'run_command',return_value=self.sha), \
             patch.object(core,'Jira',FakeJira):
            rev.perform(self.cfg,self.s)
        self.assertEqual(self.s['stage'],'passed')
        self.assertEqual(self.s['reviewed_sha'],self.sha)
        self.assertEqual(self.s['review_rounds'],1)
        published.assert_called_once()
        self.assertIn('https://github.com/a/b/pull/12',jira_comments[0][1])
        self.assertIn('Human approval',jira_comments[0][1])

    def test_changes_requested_stops_for_next_tick(self):
        self.s.update(stage='publish',reviewed_sha=self.sha, result={
            'verdict':'changes_requested','summary':'needs fix','findings':[{
                'id':'R1','severity':'high','path':'lib/game.dart','line':2,'body':'incorrect state'}]})
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info), \
             patch.object(rev,'publish_review'):
            rev.perform(self.cfg, self.s)
        self.assertEqual(self.s['stage'], 'fix_prepare')
        self.assertEqual(self.s['fix_rounds'],0)

    def test_external_pr_change_invalidates_old_review_output(self):
        self.s.update(stage='review',review_running_sha='b'*40,review_completed=True,
                      review_session='old-session')
        logdir = rev.review_dir(self.cfg) / 'logs'
        logdir.mkdir()
        (logdir / 'SSG-4.review.json').write_text(json.dumps({'verdict':'pass','summary':'old','findings':[]}))
        captured=[]
        def agent(cfg,state,path,prompt,role,output,schema=None):
            captured.append(state.get('review_session'))
            output.write_text(json.dumps({'verdict':'pass','summary':'new','findings':[]}))
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info), \
             patch.object(rev,'begin_worktree',return_value=self.fake_path), \
             patch.object(rev,'agent_run',side_effect=agent), \
             patch.object(rev,'publish_review'), \
             patch.object(rev,'pr_checks',return_value='passed'), \
             patch.object(rev,'forget_worktree'), \
             patch.object(core,'run_command',return_value=self.sha), \
             patch.object(core,'Jira') as jira:
            jira.return_value.has_comment_marker.return_value=True
            rev.perform(self.cfg,self.s)
        self.assertEqual(captured,[None])
        self.assertEqual(self.s['reviewed_sha'],self.sha)
        self.assertEqual(self.s['stage'],'passed')

    def test_recover_crash_after_push_without_misclassifying_as_external(self):
        new_sha='c'*40
        self.info['head']['sha']=new_sha
        self.s.update(stage='fix_push',reviewed_sha=self.sha)
        fix = rev.review_dir(self.cfg) / 'worktrees' / 'SSG-4-fix'
        fix.mkdir(parents=True)
        cmds=[]
        def git(args, **kw):
            cmds.append(args)
            if 'rev-parse' in args:
                return new_sha
            return ''
        with patch.object(rev,'repository_name',return_value='a/b'), \
             patch.object(rev,'pr_info',return_value=self.info), \
             patch.object(core,'run_command',side_effect=git), \
             patch.object(rev,'forget_worktree'):
            rev.perform(self.cfg,self.s)
        self.assertEqual(self.s['stage'],'review')
        self.assertIsNone(self.s['reviewed_sha'])
        self.assertFalse(any('push' in x for x in cmds))


if __name__=='__main__':
    unittest.main()
