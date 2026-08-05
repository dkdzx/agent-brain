from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_brain.long_term_memory import (
    command_add,
    command_query,
    command_supersede,
    command_verify,
)


class MemoryTest(unittest.TestCase):
    def test_add_query_supersede_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = dict(
                root=str(root),
                author_type="controller",
                agent_id="controller-001",
                source_thread="synthetic-thread",
                task_id="synthetic-task",
                status="active",
                confidence=0.9,
                valid_at=None,
                evidence_ref=["synthetic/evidence.json"],
                supersedes=[],
                scope=["module-a"],
            )
            command_add(
                argparse.Namespace(
                    **base,
                    memory_id="memory-001",
                    content="The interface contract is provisional.",
                )
            )
            command_add(
                argparse.Namespace(
                    **base,
                    memory_id="memory-002",
                    content="The interface contract is stable.",
                )
            )
            command_supersede(
                argparse.Namespace(
                    root=str(root),
                    old_memory_id="memory-001",
                    new_memory_id="memory-002",
                )
            )
            query = command_query(
                argparse.Namespace(
                    root=str(root), text="interface contract", limit=20
                )
            )
            self.assertEqual(query["count"], 2)
            statuses = {row["memory_id"]: row["status"] for row in query["memories"]}
            self.assertEqual(statuses["memory-001"], "superseded")
            self.assertEqual(statuses["memory-002"], "active")
            self.assertEqual(
                command_verify(argparse.Namespace(root=str(root)))["status"], "PASS"
            )


if __name__ == "__main__":
    unittest.main()
