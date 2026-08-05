#!/usr/bin/env python3
"""Build Graphiti import requests without performing live database writes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-index", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    index = json.loads(Path(args.memory_index).read_text(encoding="utf-8"))
    requests = []
    for memory in index.get("memories", {}).values():
        if memory.get("status") not in {"active", "uncertain"}:
            continue
        episode = {
            "name": memory["memory_id"],
            "episode_body": memory["content"],
            "source_description": "agent-brain reviewed long-term memory",
            "reference_time": memory["valid_at"],
            "metadata": {
                "agent_id": memory["agent_id"],
                "task_id": memory["task_id"],
                "source_thread": memory["source_thread"],
                "confidence": memory["confidence"],
                "status": memory["status"],
                "scope": memory["scope"],
                "evidence_refs": memory["evidence_refs"],
            },
        }
        episode["request_hash"] = hashlib.sha256(
            canonical(episode).encode("utf-8")
        ).hexdigest()
        requests.append(episode)
    output = {
        "schema_version": "agent_brain_graphiti_import_requests_v1",
        "live_writes_performed": 0,
        "request_count": len(requests),
        "requests": requests,
    }
    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(canonical({"status": "CREATED", "request_count": len(requests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

