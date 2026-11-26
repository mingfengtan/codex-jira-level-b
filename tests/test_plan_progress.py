"""Live plan logger tests use local Python subprocesses, not Codex/network."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import run_plan_codex


class PlanProgressTests(unittest.TestCase):
    def test_stream_logs_events_and_prints_status(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            log = directory / "events.jsonl"
            script = (
                "import json,sys; "
                "print(json.dumps({'type':'thread.started','thread_id':'session-42'}),flush=True); "
                "print(json.dumps({'type':'turn.started'}),flush=True); "
                "print(json.dumps({'type':'turn.completed'}),flush=True)"
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                run_plan_codex([sys.executable, "-u", "-c", script, "prompt"],
                               directory, 5, log, heartbeat_seconds=0.001)
            events = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual(events[0]["type"], "orchestrator.start")
            self.assertIn("thread.started", [e["type"] for e in events])
            self.assertEqual(events[-1]["type"], "orchestrator.exit")
            self.assertEqual(events[-1]["returncode"], 0)
            self.assertIn("session-42", stdout.getvalue())
            self.assertIn("Codex turn completed", stdout.getvalue())

    def test_non_json_stderr_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            log = directory / "events.jsonl"
            script = "print('a plain stderr line'); print('{\"type\":\"turn.completed\"}')"
            with contextlib.redirect_stdout(io.StringIO()):
                run_plan_codex([sys.executable, "-u", "-c", script, "prompt"],
                               directory, 5, log)
            events = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertIn("orchestrator.non_json_output", [e["type"] for e in events])

    def test_timeout_preserves_log(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            log = directory / "events.jsonl"
            script = "import time; print('{\"type\":\"turn.started\"}', flush=True); time.sleep(8)"
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    run_plan_codex([sys.executable, "-u", "-c", script, "prompt"],
                                   directory, 0.25, log, heartbeat_seconds=0.1)
            self.assertIn("orchestrator.timeout", log.read_text())
            # A very short timeout can fire before child Python starts.


if __name__ == "__main__":
    unittest.main()
