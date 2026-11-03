#!/usr/bin/env python3
"""Refuse unreviewed changes to known Run lifecycle writer contexts.

This bounded AST witness gate supplements actual endpoint and authority tests.
It cannot prove absence of arbitrary dynamic SQL or future aliasing patterns.
"""
import ast
import hashlib
import json
from pathlib import Path
if __package__:
    from .audit_run_writers import inventory
else:
    from audit_run_writers import inventory
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "contracts/run-writer-retirement-v1.json"


def check(manifest, current):
    errors = []
    if (manifest.get("schema_version") != 1 or manifest.get("complete_for_reviewed_scope") is not True
            or not manifest.get("review_scope") or not isinstance(manifest.get("sites"), list)):
        return ["The bounded Run writer review is incomplete."]
    expected = {row["identity"]: row for row in manifest["sites"]}
    if len(expected) != len(manifest["sites"]):
        errors.append("Duplicate reviewed writer identity.")
    actual = {row["identity"]: row for row in current["sites"]}
    for key in sorted(expected.keys() - actual.keys()):
        errors.append("Removed or changed writer requires review: " + key)
    for key, row in actual.items():
        reviewed = expected.get(key)
        if reviewed is None:
            errors.append("Unreviewed Run writer: " + key)
        elif (reviewed.get("function_sha256") != row["function_sha256"]
                or not reviewed.get("classification") or reviewed["classification"].startswith(("pending", "signed_fix_pending", "needs_manual"))
                or reviewed.get("pending_owner") or not reviewed.get("reason") or not reviewed.get("guard")):
            errors.append("Writer or its authority context requires review: " + key)
    return errors


def check_control_contexts(root, contexts):
    """Pin separately reviewed effect callers the writer heuristic cannot infer."""
    errors = []
    for context in contexts:
        path, qualified = context["path"], context["function"]
        tree = ast.parse((Path(root) / path).read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names, parent = [node.name], node
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(parent.name)
            if ".".join(reversed(names)) == qualified:
                found.append(hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())
        if (found != [context.get("function_sha256")] or not context.get("reason")
                or not context.get("test_refs")):
            errors.append("Execution control context requires review: " + path + ":" + qualified)
    return errors


def main():
    try:
        manifest = json.loads((ROOT / MANIFEST).read_text())
        current = inventory(ROOT)
        errors = check(manifest, current)
        errors.extend(check_control_contexts(ROOT, manifest.get("control_contexts", [])))
    except (OSError, ValueError, KeyError, TypeError):
        print("Run writer review unavailable or malformed.")
        return 1
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Run writer review: {len(current['sites'])} exact writer contexts match the bounded manual review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
