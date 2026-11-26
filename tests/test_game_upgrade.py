import unittest
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import Jira, apply_plan, bootstrap, once, read_plan, validate_plan


class TestGameUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sample = Path(__file__).resolve().parent.parent / 'plan.example.game.yaml'

    def test_sample_game_plan_16_items_and_p0_p1_only(self):
        plan = read_plan(self.sample)
        validate_plan(plan, 'AIDEV')
        self.assertEqual(sum(len(e['tasks']) for e in plan['epics']), 16)
        self.assertEqual({e['phase'] for e in plan['epics']}, {'P0', 'P1'})
        self.assertEqual(plan['epics'][0]['tasks'][0]['id'], 'flutter-bootstrap')

    def test_nested_subtasks_valid_and_parent_dependency_rejected(self):
        plan = {
            'project': 'AIDEV',
            'epics': [{'id': 'ep', 'summary': 'Epic', 'description': 'Desc', 'tasks': [{
                'id': 'parent', 'summary': 'Parent', 'description': 'Parent desc',
                'type': 'Story', 'acceptance_criteria': ['yes'], 'depends_on': [],
                'subtasks': [{'id': 'child', 'summary': 'Child', 'description': 'Child desc',
                              'acceptance_criteria': ['test'], 'depends_on': []}]
            }]}]
        }
        validate_plan(plan, 'AIDEV')
        plan['epics'][0]['tasks'][0]['subtasks'][0]['depends_on'] = ['parent']
        with self.assertRaisesRegex(ValueError, 'parent'):
            validate_plan(plan, 'AIDEV')

    def test_nested_subtasks_create_once_without_approval_label(self):
        from orchestrator import write_plan
        plan = {'project':'AIDEV','epics':[{'id':'ep','summary':'Epic','description':'Desc',
            'tasks':[{'id':'parent','summary':'P','description':'P','type':'Story',
                      'acceptance_criteria':['ok'],'depends_on':[],
                      'subtasks':[{'id':'child','summary':'Child','description':'Child',
                                   'acceptance_criteria':['ok'],'depends_on':[]}]}]}]}
        path = self.root/'plan.yaml'
        write_plan(path,plan)
        created=[]
        class FakeJira:
            def __init__(self, cfg): pass
            def create_issue(self,summary,description,issue_type,parent=None,labels=None):
                selfkey=f'AIDEV-{len(created)+1}'
                created.append((selfkey,issue_type,parent,labels))
                return selfkey
            def blocks(self,*args): raise AssertionError('no links expected')
        with patch('orchestrator.Jira',FakeJira):
            apply_plan(SimpleNamespace(project='AIDEV'),path)
            apply_plan(SimpleNamespace(project='AIDEV'),path)
        self.assertEqual(len(created),3)
        self.assertEqual(created[1][1:3],('Story','AIDEV-1'))
        self.assertEqual(created[2][1:3],('Subtask','AIDEV-2'))
        self.assertIsNone(created[2][3])

    def test_jira_resolves_site_specific_subtask_id(self):
        class MockJira(Jira):
            def __init__(self): self.cfg = SimpleNamespace(project='AIDEV')
            def call(self,method,path,data=None):
                if path == '/project/AIDEV':
                    return {'issueTypes':[{'id':'7','name':'Sub-task','subtask':True}]}
                if path == '/issue':
                    self.fields = data['fields']
                    return {'key':'AIDEV-11'}
                raise AssertionError(path)
        j=MockJira()
        result=j.create_issue('Example','desc','Subtask',parent='AIDEV-2')
        self.assertEqual(result,'AIDEV-11')
        self.assertEqual(j.fields['issuetype'],{'id':'7'})
        self.assertEqual(j.fields['labels'],['codex-planned'])

    def test_bootstrap_reuses_plan_no_new_codex_call(self):
        path=self.root/'plan.yaml'
        path.write_text(self.sample.read_text())
        with patch('orchestrator.ensure_git_repo'), patch('orchestrator.plan_command') as gen, patch('orchestrator.apply_plan') as ap:
            bootstrap(SimpleNamespace(project='AIDEV'),self.root/'brief.md',path)
            gen.assert_not_called()
            ap.assert_called_once()

    def test_once_skips_parent_ticket_with_children(self):
        class FakeJira:
            def __init__(self,cfg): pass
            def ready_issues(self): return [{'key':'AIDEV-1'},{'key':'AIDEV-2'}]
            def issue(self,key):
                return {'key':key,'fields':{'summary':key,'issuetype':{'name':'Story'},
                    'subtasks':[{'key':'AIDEV-9'}] if key.endswith('1') else []}}
            def dependencies_satisfied(self,issue): return True
        cfg=SimpleNamespace(repo=self.root,worktree_root=self.root)
        with patch('orchestrator.ensure_git_repo'),patch('orchestrator.Jira',FakeJira):
            with patch('builtins.print') as pr:
                once(cfg,True)
            lines='\n'.join(str(x) for x in pr.call_args_list)
            self.assertIn('parent has subtasks',lines)
            self.assertIn('AIDEV-2',lines)


if __name__ == '__main__': unittest.main()

class TestPhaseSafety(unittest.TestCase):
    def test_bootstrap_blocks_phase_p3_plan(self):
        from tempfile import TemporaryDirectory
        from orchestrator import bootstrap, write_plan
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'plan.yaml'
            plan={'project':'AIDEV','epics':[{'id':'commerce','summary':'Commerce',
                'description':'Later','phase':'P3','tasks':[]}]}
            write_plan(path,plan)
            with patch('orchestrator.ensure_git_repo'), patch('orchestrator.apply_plan') as apply:
                with self.assertRaisesRegex(ValueError,'outside P0/P1'):
                    bootstrap(SimpleNamespace(project='AIDEV'), Path(tmp)/'brief.md', path)
                apply.assert_not_called()
