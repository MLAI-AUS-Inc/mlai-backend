"""Prevent newly added test modules from disappearing outside CI selections.

The dated baseline records existing selection debt, not passing tests or a
quarantine of known failures. Partial class/method selections are reported.
"""

import ast
from datetime import date
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/ci-unassigned-baseline.json"


def test_modules(root=ROOT):
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=root, text=True,
    ).splitlines()
    modules = set()
    for name in set(names):
        if "/migrations/" in name:
            continue
        path = root / name
        if not path.is_file() or not path.name.startswith("test") or name.startswith("scripts/"):
            continue
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
            for node in ast.walk(tree)
        ):
            modules.add(name.removesuffix(".py").replace("/", "."))
    return modules


def selected_labels(workflow):
    logical = workflow.replace("\\\n", " ")
    pattern = r"python(?:3)? (?:manage\.py test|-m unittest|scripts/test_without_database\.py) ([^\n]+)"
    labels = set()
    for command in re.findall(pattern, logical):
        for token in shlex.split(command):
            if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", token):
                labels.add(token)
    return labels


def classify(modules, labels):
    complete, partial, absent = set(), set(), set()
    for module in modules:
        if any(module == label or module.startswith(label + ".") for label in labels):
            complete.add(module)
        elif any(label.startswith(module + ".") for label in labels):
            partial.add(module)
        else:
            absent.add(module)
    return complete, partial, absent


def main():
    modules = test_modules()
    labels = selected_labels((ROOT / ".github/workflows/deploy.yml").read_text())
    complete, partial, absent = classify(modules, labels)
    baseline = json.loads(BASELINE.read_text())
    if date.today() > date.fromisoformat(baseline["review_by"]):
        raise SystemExit("Existing CI selection debt is due for review; assign suites before extending the baseline.")
    allowed = set(baseline["unassigned_modules"])
    new = absent - allowed
    print(
        f"CI test-module assignment: {len(complete)} fully selected, "
        f"{len(partial)} partially selected, {len(absent)} baseline omissions, "
        f"{len(new)} new omissions."
    )
    if new:
        print("Assign these modules to a CI test command:\n" + "\n".join(sorted(new)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
