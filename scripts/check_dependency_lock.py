"""Reject requirements inputs changed without regenerating the shared lock."""

import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("requirements.txt", "requirements-engine.txt")
PREFIX = "# requirements-input-sha256: "


def signature():
    digest = hashlib.sha256()
    for name in INPUTS:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes() + b"\0")
    return PREFIX + digest.hexdigest()


def main():
    path = ROOT / "requirements.lock"
    content = path.read_text()
    expected = signature()
    if sys.argv[1:] == ["--record-inputs"]:
        content = re.sub(r"^# requirements-input-sha256: .*\n", "", content, flags=re.M)
        path.write_text(expected + "\n" + content)
        return 0
    if sys.argv[1:]:
        raise SystemExit("Usage: check_dependency_lock.py [--record-inputs]")
    if expected not in content.splitlines():
        raise SystemExit("Requirements inputs changed. Regenerate requirements.lock from both inputs, then record its input signature.")
    print("Dependency lock matches both requirements inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
