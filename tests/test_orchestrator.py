import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import adf, plain, validate_plan


class TestHelpers(unittest.TestCase):
    def test_adf_roundtrip(self):
        text = "Alpha\nBeta"
        self.assertEqual(plain(adf(text)), text)

    def test_plan_valid(self):
        validate_plan({"project": "APP", "epics": [
            {"id": "one", "summary": "Epic", "description": "E", "tasks": [
                {"id": "a", "summary": "Task A", "description": "A", "acceptance_criteria": ["ok"], "depends_on": []},
                {"id": "b", "summary": "Task B", "description": "B", "acceptance_criteria": ["ok"], "depends_on": ["a"]},
            ]}
        ]}, "APP")

    def test_dependency_cycle_rejected(self):
        plan = {"project": "APP", "epics": [{"id": "epic", "summary": "Epic", "description": "E", "tasks": [
            {"id": "a", "summary": "A", "description": "A", "acceptance_criteria": ["yes"], "depends_on": ["b"]},
            {"id": "b", "summary": "B", "description": "B", "acceptance_criteria": ["yes"], "depends_on": ["a"]},
        ]}]}
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan(plan, "APP")



class TestJiraLogic(unittest.TestCase):
    def test_blocked_dependency(self):
        from orchestrator import Jira
        jira = object.__new__(Jira)
        jira.issue = lambda key: {"fields": {"status": {"statusCategory": {"key": "indeterminate"}}}}
        ticket = {"fields": {"issuelinks": [{
            "type": {"inward": "is blocked by"}, "inwardIssue": {"key": "APP-1"}
        }]}}
        self.assertFalse(jira.dependencies_satisfied(ticket))

    def test_done_dependency(self):
        from orchestrator import Jira
        jira = object.__new__(Jira)
        jira.issue = lambda key: {"fields": {"status": {"statusCategory": {"key": "done"}}}}
        ticket = {"fields": {"issuelinks": [{
            "type": {"inward": "is blocked by"}, "inwardIssue": {"key": "APP-1"}
        }]}}
        self.assertTrue(jira.dependencies_satisfied(ticket))




class TestYamlPlans(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.dir = Path(self.temp_dir.name)
        self.example = Path(__file__).resolve().parent.parent / "plan.example.yaml"

    def test_example_plan_valid_and_cross_epic_dependency(self):
        from orchestrator import read_plan
        plan = read_plan(self.example)
        self.assertEqual(len(plan["epics"]), 2)
        self.assertEqual(plan["epics"][1]["tasks"][0]["depends_on"], ["auth-api"])
        validate_plan(plan, "AIDEV")

    def test_yaml_round_trip(self):
        from orchestrator import read_plan, write_plan
        plan = read_plan(self.example)
        path = self.dir / "plan.yml"
        write_plan(path, plan)
        self.assertEqual(read_plan(path), plan)

    def test_json_compatibility(self):
        from orchestrator import read_plan, write_plan
        plan = read_plan(self.example)
        path = self.dir / "plan.json"
        write_plan(path, plan)
        self.assertEqual(read_plan(path), plan)

    def test_safe_load_blocks_python_objects(self):
        from orchestrator import read_plan
        path = self.dir / "plan.yaml"
        path.write_text("!!python/object/apply:os.system ['echo unsafe']\n")
        with self.assertRaisesRegex(ValueError, "Invalid plan file"):
            read_plan(path)

    def test_yaml_non_mapping_rejected(self):
        from orchestrator import read_plan
        path = self.dir / "plan.yaml"
        path.write_text("- hi\n- bye\n")
        with self.assertRaisesRegex(ValueError, "mapping"):
            read_plan(path)

    def test_invalid_dependency_rejected(self):
        from orchestrator import read_plan
        plan = read_plan(self.example)
        plan["epics"][0]["tasks"][0]["depends_on"] = ["not-a-task"]
        with self.assertRaisesRegex(ValueError, "Invalid dependency"):
            validate_plan(plan, "AIDEV")

    def test_schema_is_yaml_and_can_validate_plan(self):
        from orchestrator import SCHEMA, load_plan_schema, validate_plan_schema, read_plan
        self.assertEqual(SCHEMA.suffix, ".yaml")
        schema = load_plan_schema()
        self.assertEqual(schema["properties"]["epics"]["type"], "array")
        validate_plan_schema(read_plan(self.example), schema)

    def test_schema_rejects_unknown_field(self):
        from orchestrator import read_plan
        plan = read_plan(self.example)
        plan["epics"][0]["tasks"][0]["unexpected"] = 99
        with self.assertRaisesRegex(ValueError, "Schema validation failed"):
            validate_plan(plan, "AIDEV")

    def test_schema_rejects_incorrect_field_type(self):
        from orchestrator import read_plan
        plan = read_plan(self.example)
        plan["epics"][0]["tasks"][0]["acceptance_criteria"] = "not a list"
        with self.assertRaisesRegex(ValueError, "Schema validation failed"):
            validate_plan(plan, "AIDEV")

    def test_schema_rejects_missing_required_field(self):
        from orchestrator import read_plan
        plan = read_plan(self.example)
        del plan["epics"][0]["tasks"][0]["description"]
        with self.assertRaisesRegex(ValueError, "Schema validation failed"):
            validate_plan(plan, "AIDEV")

    def test_schema_loader_rejects_invalid_yaml(self):
        from orchestrator import load_plan_schema
        path = self.dir / "broken.schema.yaml"
        path.write_text("properties: [broken\n")
        with self.assertRaisesRegex(ValueError, "Cannot read YAML schema"):
            load_plan_schema(path)

    def test_plan_command_converts_yaml_schema_to_temporary_json(self):
        import json
        from unittest.mock import patch
        from orchestrator import plan_command, read_plan
        example = read_plan(self.example)
        class Cfg:
            project = "AIDEV"
            repo = self.dir
            codex_timeout = 3
        brief = self.dir / "brief.md"
        brief.write_text("Build a small app")
        output = self.dir / "plan.yaml"
        def fake_run(cmd, **kwargs):
            from orchestrator import load_plan_schema
            output_schema_path = Path(cmd[cmd.index("--output-schema") + 1])
            self.assertEqual(output_schema_path.suffix, ".json")
            self.assertEqual(json.loads(output_schema_path.read_text()), load_plan_schema())
            destination = Path(cmd[cmd.index("--output-last-message") + 1])
            destination.write_text(json.dumps(example))
            return ""
        with patch("orchestrator.run_command", side_effect=fake_run) as call:
            plan_command(Cfg(), brief, output)
        self.assertEqual(read_plan(output), example)
        self.assertIn("--output-schema", call.call_args.args[0])
        self.assertIn("codex", call.call_args.args[0][0])

    def test_apply_plan_yaml_creates_epics_tasks_and_links_once(self):
        from unittest.mock import patch
        from orchestrator import apply_plan
        class Cfg:
            project = "AIDEV"
        plan_path = self.dir / "plan.yaml"
        plan_path.write_text(self.example.read_text())
        created = []
        links = []
        class FakeJira:
            def __init__(self, cfg):
                self.cfg = cfg
            def create_issue(self, summary, description, issue_type, parent=None, labels=None):
                created.append((summary, issue_type, parent, description))
                return f"AIDEV-{len(created)}"
            def blocks(self, blocker, blocked):
                links.append((blocker, blocked))
        with patch("orchestrator.Jira", FakeJira):
            apply_plan(Cfg(), plan_path)
            apply_plan(Cfg(), plan_path)
        self.assertEqual(len(created), 5)
        self.assertEqual(len(links), 2)
        self.assertEqual(created[0][1], "Epic")
        self.assertEqual(created[1][1], "Task")
        self.assertEqual(created[1][2], "AIDEV-1")
        self.assertEqual(links, [("AIDEV-2", "AIDEV-3"), ("AIDEV-3", "AIDEV-5")])
        self.assertTrue((self.dir / "plan.applied.json").exists())


if __name__ == "__main__":
    unittest.main()
