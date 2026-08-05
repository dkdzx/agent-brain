from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class DemoTest(unittest.TestCase):
    def test_three_agent_demo(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPO / "examples" / "three_agent_demo.py")],
            cwd=REPO,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["demo_status"], "PASS")
        self.assertEqual(result["runtime_state"], "ARCHIVED")
        self.assertEqual(result["active_members"], 0)
        self.assertEqual(result["verification"], "PASS")


if __name__ == "__main__":
    unittest.main()

