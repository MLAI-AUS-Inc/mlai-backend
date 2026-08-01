#!/usr/bin/env python3
"""Fail CI when tracked source contains high-confidence credential material."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PATTERNS = {
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Stripe live key": re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
}

PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----(?:\\n|\r?\n)"
    r"[A-Za-z0-9+/=\s\\]{80,}"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
)

SKIP_SUFFIXES = {
    ".bmp",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite3",
    ".ttf",
    ".woff",
    ".woff2",
}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def scan(root: Path) -> list[tuple[Path, int, str]]:
    findings = []
    for path in tracked_files(root):
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        private_key_match = PRIVATE_KEY_BLOCK.search(content)
        if private_key_match:
            line_number = content.count("\n", 0, private_key_match.start()) + 1
            findings.append((path.relative_to(root), line_number, "private key"))
        lines = content.splitlines()
        for line_number, line in enumerate(lines, start=1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((path.relative_to(root), line_number, label))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan(root)
    if findings:
        for path, line_number, label in findings:
            print(f"{path}:{line_number}: possible {label}", file=sys.stderr)
        print("Remove or rotate detected credentials before committing.", file=sys.stderr)
        return 1
    print("Tracked-source credential scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
