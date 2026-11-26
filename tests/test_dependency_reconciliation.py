"""Regression tests for Jira issue-link direction and safe repairs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import Jira, audit_dependencies, write_plan


class TestBlocksDirection(unittest.TestCase):
    def test_create_blocks_link_uses_jira_link_end_conventions(self):
        jira = object.__new__(Jira)
        requests = []
        jira.call = lambda method, path, data=None: requests.append((method, path, data)) or {}
        jira.blocks('SSG-2', 'SSG-3')
        self.assertEqual(requests, [('POST', '/issueLink', {
            'type': {'name': 'Blocks'},
            'outwardIssue': {'key': 'SSG-3'},
            'inwardIssue': {'key': 'SSG-2'},
        })])

    def test_remove_link_uses_real_link_id(self):
        jira = object.__new__(Jira)
        requests = []
        jira.call = lambda method, path, data=None: requests.append((method, path)) or {}
        jira.remove_link('12345')
        self.assertEqual(requests, [('DELETE', '/issueLink/12345')])


class TestAuditAndRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / 'plan.yaml'
        self.plan = {'project': 'SSG', 'epics': [{
            'id': 'foundation', 'summary': 'Foundation', 'description': 'Foundation',
            'tasks': [
                {'id': 'SSG-001', 'summary': 'Flutter', 'description': 'Flutter scaffold',
                 'acceptance_criteria': ['Runs'], 'depends_on': []},
                {'id': 'SSG-002', 'summary': 'Flame', 'description': 'Flame integration',
                 'acceptance_criteria': ['Runs'], 'depends_on': ['SSG-001']},
                {'id': 'SSG-003', 'summary': 'CI', 'description': 'CI',
                 'acceptance_criteria': ['Tests pass'], 'depends_on': ['SSG-002']},
            ]}]}
        write_plan(self.path, self.plan)
        state = {'issues': {'foundation': 'SSG-1', 'SSG-001': 'SSG-2',
                            'SSG-002': 'SSG-3', 'SSG-003': 'SSG-4'},
                 'links': ['SSG-001>SSG-002', 'SSG-002>SSG-003']}
        (self.dir / 'plan.applied.json').write_text(json.dumps(state))
        self.cfg = SimpleNamespace(project='SSG')

    class FakeJira:
        def __init__(self):
            self.links = {'SSG-3': [
                {'id': '101', 'type': {'name': 'Blocks'}, 'outwardIssue': {'key': 'SSG-2'}},
                # An unrelated link must survive repairs.
                {'id': '900', 'type': {'name': 'Blocks'}, 'outwardIssue': {'key': 'SSG-99'}},
            ], 'SSG-4': []}
            self.created = []
            self.deleted = []

        def issue(self, key):
            return {'fields': {'issuelinks': self.links.get(key, [])}}

        def blocks(self, blocker, blocked):
            self.created.append((blocker, blocked))
            self.links.setdefault(blocked, []).append({
                'id': str(200 + len(self.created)), 'type': {'name': 'Blocks'},
                'inwardIssue': {'key': blocker}
            })

        def remove_link(self, link_id):
            self.deleted.append(link_id)
            for key in self.links:
                self.links[key] = [x for x in self.links[key] if x['id'] != link_id]

    def test_readonly_audit_does_not_modify_jira(self):
        jira = self.FakeJira()
        with patch('orchestrator.Jira', return_value=jira), patch('builtins.print') as printer:
            audit_dependencies(self.cfg, self.path, fix=False)
        self.assertEqual(jira.deleted, [])
        self.assertEqual(jira.created, [])
        output = '\n'.join(str(x) for x in printer.call_args_list)
        self.assertIn('REVERSED', output)
        self.assertIn('MISSING', output)

    def test_repair_then_reaudit_is_idempotent(self):
        jira = self.FakeJira()
        with patch('orchestrator.Jira', return_value=jira):
            audit_dependencies(self.cfg, self.path, fix=True)
            audit_dependencies(self.cfg, self.path, fix=True)
        self.assertEqual(jira.deleted, ['101'])
        self.assertEqual(jira.created, [('SSG-2','SSG-3'), ('SSG-3','SSG-4')])
        self.assertTrue(any(link.get('id') == '900' for link in jira.links['SSG-3']))

    def test_no_mapping_file_no_writes(self):
        (self.dir / 'plan.applied.json').unlink()
        jira = self.FakeJira()
        with patch('orchestrator.Jira', return_value=jira):
            with self.assertRaisesRegex(ValueError, 'Missing .*plan.applied.json'):
                audit_dependencies(self.cfg, self.path, fix=True)
        self.assertEqual(jira.deleted, [])
        self.assertEqual(jira.created, [])

    def test_correct_link_is_kept(self):
        jira = self.FakeJira()
        jira.links['SSG-3'] = [{'id':'101','type':{'name':'Blocks'},'inwardIssue':{'key':'SSG-2'}}]
        jira.links['SSG-4'] = [{'id':'102','type':{'name':'Blocks'},'inwardIssue':{'key':'SSG-3'}}]
        with patch('orchestrator.Jira', return_value=jira):
            audit_dependencies(self.cfg, self.path, fix=True)
        self.assertEqual(jira.deleted, [])
        self.assertEqual(jira.created, [])


if __name__ == '__main__':
    unittest.main()
