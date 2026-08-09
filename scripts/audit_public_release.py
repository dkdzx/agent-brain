#!/usr/bin/env python3
"""Fail if the public tree contains common private-project markers."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".txt",
    ".ps1",
    ".gitignore",
}
FORBIDDEN = {
    "private_project_name": re.compile(r"大衡|Daheng|daheng"),
    "private_workspace": re.compile(r"WORLD_MAP_V1|D:\\模拟器|/home/[^/]+/"),
    "private_local_user": re.compile(r"lenovo|C:\\Users\\"),
    "real_thread_id": re.compile(r"\b019f[0-9a-f-]{20,}\b", re.I),
    "raw_transcript": re.compile(r"raw_chat_transcript\s*[:=]\s*[\"']?[^\s,}]+"),
    "credential": re.compile(
        r"(?<![A-Za-z0-9])(?:gho_[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|Authorization:\s*Bearer)",
        re.I,
    ),
}


def main() -> int:
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                hits.append(
                    {
                        "file": str(path.relative_to(ROOT)),
                        "rule": label,
                        "match": match.group(0)[:120],
                    }
                )
    if hits:
        for hit in hits:
            print(f"FAIL {hit['rule']} {hit['file']}: {hit['match']}")
        return 1
    print("PASS: no private-project markers found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
