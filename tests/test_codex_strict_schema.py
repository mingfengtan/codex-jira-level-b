"""Regression checks for Codex Structured Outputs --output-schema.

The API rejects schemas containing optional properties on *any* object level.
"""
import copy
import unittest

from jsonschema import Draft202012Validator

from orchestrator import codex_output_schema, load_plan_schema, read_plan


class StrictCodexSchemaTests(unittest.TestCase):
    def test_every_nested_object_requires_all_properties(self):
        schema = codex_output_schema(load_plan_schema())

        def visit(node):
            if not isinstance(node, dict):
                return
            if node.get("type") == "object":
                self.assertEqual(node.get("additionalProperties"), False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
            for child in node.get("properties", {}).values():
                visit(child)
            visit(node.get("items"))

        visit(schema)
        # This catches the exact failure in Codex: missing required 'phase'.
        epic_item = schema["properties"]["epics"]["items"]
        self.assertIn("phase", epic_item["required"])
        task_item = epic_item["properties"]["tasks"]["items"]
        self.assertIn("type", task_item["required"])
        self.assertIn("priority", task_item["required"])
        self.assertIn("subtasks", task_item["required"])
        child_item = task_item["properties"]["subtasks"]["items"]
        self.assertIn("priority", child_item["required"])

    def test_conversion_does_not_mutate_local_schema(self):
        original = load_plan_schema()
        before = copy.deepcopy(original)
        codex_output_schema(original)
        self.assertEqual(original, before)
        epic_item = original["properties"]["epics"]["items"]
        self.assertNotIn("phase", epic_item["required"])

    def test_generated_full_plan_validates_with_strict_schema(self):
        plan = read_plan(__import__('pathlib').Path(__file__).resolve().parent.parent / 'plan.example.game.yaml')
        # Codex's output must have all fields, including empty subtasks.
        for epic in plan["epics"]:
            for task in epic["tasks"]:
                task.setdefault("type", "Task")
                task.setdefault("priority", "Medium")
                task.setdefault("subtasks", [])
                for subtask in task["subtasks"]:
                    subtask.setdefault("priority", "Medium")
        Draft202012Validator(codex_output_schema(load_plan_schema())).validate(plan)


if __name__ == '__main__':
    unittest.main()
