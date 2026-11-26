import os
from pathlib import Path
import unittest
from unittest.mock import patch

from orchestrator import codex_args


class CodexCliArgsTests(unittest.TestCase):
    def test_cli_exec_no_rejected_approval_argument(self):
        with patch.dict(os.environ, {"OPENAI_CODEX_BIN": "/opt/homebrew/bin/codex"}):
            args = codex_args(Path("/tmp/project"), "read-only")
        self.assertEqual(args[:2], ["/opt/homebrew/bin/codex", "exec"])
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--ask-for-approval", args)

    def test_falls_back_to_path(self):
        with patch.dict(os.environ, {}, clear=True):
            args = codex_args(Path("/tmp/project"))
        self.assertEqual(args[0], "codex")


if __name__ == "__main__":
    unittest.main()
