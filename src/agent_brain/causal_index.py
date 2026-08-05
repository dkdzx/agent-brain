#!/usr/bin/env python3
"""Build a sourced, explicitly incomplete causal index."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-index", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--gap", action="append", default=[])
    args = parser.parse_args()

    index = json.loads(Path(args.memory_index).read_text(encoding="utf-8"))
    nodes = []
    for memory in index.get("memories", {}).values():
        nodes.append(
            {
                "node_id": memory["memory_id"],
                "node_type": "memory",
                "label": memory["content"][:120],
                "status": memory["status"],
                "scope": memory["scope"],
                "source_refs": [memory["source_thread"], *memory["evidence_refs"]],
                "valid_at": memory["valid_at"],
                "invalid_at": memory["invalid_at"],
                "content_hash": memory["content_hash"],
            }
        )
    edges = []
    for memory in index.get("memories", {}).values():
        for old_id in memory.get("supersedes", []):
            edges.append(
                {
                    "edge_id": f"{memory['memory_id']}::supersedes::{old_id}",
                    "from": memory["memory_id"],
                    "to": old_id,
                    "relation": "supersedes",
                    "status": "confirmed",
                    "source_refs": [memory["source_thread"]],
                }
            )
    graph = {
        "schema_version": "agent_brain_causal_index_v1",
        "graph_status": "INCOMPLETE",
        "claim_ceiling": "This graph is a partial sourced index, not a complete world model.",
        "nodes": nodes,
        "edges": edges,
        "coverage": {
            "represented_node_count": len(nodes),
            "represented_edge_count": len(edges),
        },
        "gaps": [
            {
                "gap_id": f"gap-{i + 1:03d}",
                "description": gap,
                "status": "UNKNOWN",
            }
            for i, gap in enumerate(args.gap)
        ],
    }
    graph["semantic_hash"] = digest(graph)
    Path(args.output).write_text(
        json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "CREATED",
                "nodes": len(nodes),
                "edges": len(edges),
                "gaps": len(graph["gaps"]),
                "semantic_hash": graph["semantic_hash"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

