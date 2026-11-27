import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import run_codex_stream


class TestWorkerProgress(unittest.TestCase):
    def test_worker_streams_events_and_keeps_raw_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / 'fake_codex.py'
            runner.write_text('''import json
print(json.dumps({"type":"thread.started","thread_id":"test-session"}), flush=True)
print(json.dumps({"type":"turn.started"}), flush=True)
print(json.dumps({"type":"item.started","item":{"type":"command_execution", "command":"secret command"}}), flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"file_change", "content":"secret source code"}}), flush=True)
print(json.dumps({"type":"turn.completed"}), flush=True)
''')
            job = {"key": "SSG-2", "worktree": str(root), "session_id": None,
                   "active_pid": None, "active_kind": None, "codex_complete": False}
            cfg = SimpleNamespace(codex_timeout=10)
            log = root / 'test.log'
            last = root / 'last.md'
            printed = io.StringIO()
            with patch('orchestrator.codex_args', return_value=[sys.executable, str(runner)]), \
                 patch('orchestrator.persist_job'):
                with contextlib.redirect_stdout(printed):
                    run_codex_stream(cfg, job, 'test', log, last)
            output = printed.getvalue()
            self.assertIn('SSG-2: starting Codex', output)
            self.assertIn('Codex turn.completed', output)
            self.assertIn('Codex finished successfully', output)
            self.assertNotIn('secret source code', output)
            self.assertNotIn('secret command', output)
            self.assertIn('secret source code', log.read_text())
            self.assertTrue(job['codex_complete'])
            self.assertEqual(job['session_id'], 'test-session')
            self.assertIsNone(job['active_pid'])

    def test_running_stage_progress_is_emitted(self):
        import inspect
        from orchestrator import run_saved_job
        text = inspect.getsource(run_saved_job)
        self.assertIn('stage={phase}', text)

if __name__ == '__main__':
    unittest.main()
