#!/usr/bin/env python3
"""Generate this repo's run-status module from contracts/run-statuses.json.

The same script is vendored byte-identically in mlai-backend and
content-factory. It writes ``content_factory/run_status_contract.py`` in
mlai-backend and ``backend/run_status_contract.py`` in content-factory.

    python scripts/generate_run_status_contract.py          # rewrite the module
    python scripts/generate_run_status_contract.py --check  # fail when it is stale
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "run-statuses.json"
LIFECYCLE = ("active", "awaiting", "terminal")
GENERATOR = "scripts/generate_run_status_contract.py"


def target(root: Path = ROOT) -> Path:
    """The generated module's path for whichever repo this script runs in."""
    if (root / "backend" / "harness").is_dir():
        return root / "backend" / "run_status_contract.py"
    if (root / "content_factory").is_dir():
        return root / "content_factory" / "run_status_contract.py"
    raise SystemExit(f"{GENERATOR}: cannot tell which repo {root} is")


def load(path: Path = CONTRACT) -> dict:
    """Read the contract and check the lifecycle partitions every run status once."""
    contract = json.loads(path.read_text(encoding="utf-8"))
    statuses = [*contract["run_statuses"], *contract["run_status_platform_extensions"]]
    lifecycle = contract.get("run_status_lifecycle") or {}
    problems = []
    if set(lifecycle) != set(LIFECYCLE):
        problems.append(f"run_status_lifecycle keys must be {list(LIFECYCLE)}")
    seen: dict[str, str] = {}
    for group in LIFECYCLE:
        for status in lifecycle.get(group, []):
            if status in seen:
                problems.append(f"{status!r} is in both {seen[status]!r} and {group!r}")
            seen[status] = group
    if set(seen) != set(statuses) or len(statuses) != len(set(statuses)):
        missing = sorted(set(statuses) - set(seen))
        unknown = sorted(set(seen) - set(statuses))
        problems.append(f"lifecycle must cover each run status exactly once (missing {missing}, unknown {unknown})")
    if set(contract["run_status_external_labels"]) != set(contract["run_statuses"]):
        problems.append("run_status_external_labels must label every run status")
    if problems:
        raise SystemExit(f"{GENERATOR}: {path} is inconsistent:\n  " + "\n  ".join(problems))
    return contract


def _frozenset(values) -> str:
    rows = "".join(f"    {json.dumps(value)},\n" for value in sorted(values))
    return "frozenset({\n" + rows + "})"


def render(contract: dict) -> str:
    """The generated module's exact text."""
    lifecycle = contract["run_status_lifecycle"]
    labels = "".join(f"    {json.dumps(key)}: {json.dumps(value)},\n"
                     for key, value in contract["run_status_external_labels"].items())
    return f'''"""Run status lifecycle, generated from contracts/run-statuses.json (version {contract["_meta"]["version"]}).

Do not edit by hand. Change the contract in both repos, then run
``python {GENERATOR}``.
CI runs it with ``--check`` and fails when this file is stale.

ACTIVE: the worker owns the run's progress.
AWAITING: paused until the owner acts.
TERMINAL: stopped; only an explicit resume or recovery transition reopens it.
"""
from types import MappingProxyType

CONTRACT_VERSION = {int(contract["_meta"]["version"])}

RUN_STATUSES = {_frozenset(contract["run_statuses"])}
PLATFORM_EXTENSIONS = {_frozenset(contract["run_status_platform_extensions"])}
EXTERNAL_LABELS = MappingProxyType({{
{labels}}})

ACTIVE = {_frozenset(lifecycle["active"])}
AWAITING = {_frozenset(lifecycle["awaiting"])}
TERMINAL = {_frozenset(lifecycle["terminal"])}


def _value(status) -> str:
    return str(getattr(status, "value", status) or "").strip().lower()


def is_active(status) -> bool:
    """True while the worker owns the run's progress."""
    return _value(status) in ACTIVE


def is_awaiting(status) -> bool:
    """True while the run waits for the owner to act."""
    return _value(status) in AWAITING


def is_terminal(status) -> bool:
    """True once the run has stopped; only a resume or recovery reopens it."""
    return _value(status) in TERMINAL
'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the generated module is stale")
    args = parser.parse_args(argv)
    path = target()
    expected = render(load())
    if args.check:
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != expected:
            print(f"{path.relative_to(ROOT)} is stale; run python {GENERATOR} and commit the result.", file=sys.stderr)
            return 1
        print(f"{path.relative_to(ROOT)} matches contracts/run-statuses.json.")
        return 0
    path.write_text(expected, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
