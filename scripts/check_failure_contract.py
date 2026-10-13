#!/usr/bin/env python3
"""Audit vendored B1 bytes, exact emitters and immutable CF provenance.

Offline validation is explicit about its limit. CI must use --remote; unavailable
private-source access fails without printing tokens, response bodies or URLs.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ("contracts/failure-codes.json", "contracts/failure-envelope-v1.schema.json", "contracts/setup-delivery-v1.schema.json")
SOURCE_REPOSITORY = "drsamdonegan/content-factory"
FIELDS = {"code", "error_code", "errorCode", "blocking_code", "blockingCode", "reasonCode", "reason_code", "failure_code", "model_adapter_blocking_code"}
CONSTRUCTORS = {"WebsiteAuthorityError": 0}


class ContractCheckError(ValueError):
    """A safe, bounded diagnostic for a source/registry/emitter check."""


def canonical_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def site_key(row):
    return hashlib.sha256(json.dumps({key: row[key] for key in ("path", "function", "field", "expression")}, sort_keys=True).encode()).hexdigest()


def python_emissions(source, path):
    """Parse production emission seams, preserving exact dynamic expressions."""
    tree = ast.parse(source, filename=path)
    constants = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value

    def finite(node, seen=frozenset(), locals_=None):
        values = {**constants, **(locals_ or {})}
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set() if node.value is None else None
        if isinstance(node, ast.Name) and node.id not in seen and node.id in values:
            return finite(values[node.id], seen | {node.id}, locals_)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            class_ = next((item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == node.value.id), None)
            if class_:
                for item in class_.body:
                    if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == node.attr for target in item.targets):
                        return finite(item.value, seen, locals_)
        if isinstance(node, ast.IfExp):
            branches = [finite(node.body, seen, locals_), finite(node.orelse, seen, locals_)]
            return branches[0] | branches[1] if all(value is not None for value in branches) else None
        if isinstance(node, ast.BoolOp):
            branches = [finite(value, seen, locals_) for value in node.values]
            return set().union(*branches) if all(value is not None for value in branches) else None
        if isinstance(node, ast.JoinedStr):
            values = {""}
            for value in node.values:
                part = finite(value.value, seen, locals_) if isinstance(value, ast.FormattedValue) else finite(value, seen, locals_)
                if part is None or len(values) * len(part) > 256:
                    return None
                values = {prefix + suffix for prefix in values for suffix in part}
            return values
        return None

    rows = []
    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = []
            self.locals = []
        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            # Resolve only unique assignments in this exact function; an ambiguous
            # name remains dynamic and requires a reviewed boundary declaration.
            candidates = {}
            def function_nodes(item):
                yield item
                for child in ast.iter_child_nodes(item):
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        yield from function_nodes(child)
            for item in function_nodes(node):
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            candidates.setdefault(target.id, []).append(item.value)
            arguments = {value.arg for value in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            self.locals.append({key: values[0] if len(values) == 1 else ast.BoolOp(op=ast.Or(), values=values)
                                for key, values in candidates.items() if key not in arguments})
            self.generic_visit(node)
            self.locals.pop(); self.scope.pop()
        visit_AsyncFunctionDef = visit_FunctionDef
        def emit(self, node, field, value):
            literals = finite(value, locals_=self.locals[-1] if self.locals else None)
            rows.append({"path": path, "function": ".".join(self.scope) or "<module>", "field": field,
                         "expression": ast.unparse(value), "line": node.lineno,
                         "literals": sorted(literals) if literals is not None else None})
        def visit_Dict(self, node):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in FIELDS:
                    self.emit(node, key.value, value)
            self.generic_visit(node)
        def visit_Call(self, node):
            for keyword in node.keywords:
                if keyword.arg in FIELDS:
                    self.emit(node, keyword.arg, keyword.value)
            name = ast.unparse(node.func).split(".")[-1]
            if name in CONSTRUCTORS and len(node.args) > CONSTRUCTORS[name]:
                self.emit(node, "error_code", node.args[CONSTRUCTORS[name]])
            if name == "setdefault" and len(node.args) > 1 and isinstance(node.args[0], ast.Constant) and node.args[0].value in FIELDS:
                self.emit(node, node.args[0].value, node.args[1])
            self.generic_visit(node)
        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in FIELDS:
                    self.emit(node, target.attr, node.value)
                elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in FIELDS:
                    self.emit(node, target.slice.value, node.value)
            self.generic_visit(node)
    Visitor().visit(tree)
    return rows


def inventory(root=ROOT):
    """Cover backend website/scan/setup/generation/publication production Python.

    Backend has no JavaScript/build emitter in this scope. CF's companion scanner
    parses its build seams with pinned Acorn; cross-repo parity covers that inventory.
    """
    paths = [path for path in (root / "content_factory").rglob("*.py")
             if not path.name.startswith(("test_", "tests_")) and not set(path.parts) & {"migrations", "__pycache__"}]
    paths += [root / name for name in ("integrations/services/article_generation.py", "integrations/api_views_content_factory_app.py")]
    return [row for path in sorted(paths) if path.is_file()
            for row in python_emissions(path.read_text(), path.relative_to(root).as_posix())]


def check_registry(root=ROOT):
    registry = json.loads((root / ARTIFACTS[0]).read_text())
    schema = json.loads((root / ARTIFACTS[1]).read_text())
    for path, value in ((root / ARTIFACTS[0], registry), (root / ARTIFACTS[1], schema)):
        if path.read_bytes() != canonical_bytes(value):
            raise ContractCheckError("noncanonical_contract_bytes")
    classes = {"transient", "waiting", "repairable", "strategy", "needs_user", "unsupported"}
    if set(registry.get("classes", [])) != classes or type(registry.get("registry_version")) is not int or registry["registry_version"] != 1:
        raise ContractCheckError("invalid_registry_version")
    aliases = set()
    for entry in registry.get("codes", []):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", entry.get("code", "")):
            raise ContractCheckError("invalid_registry_code")
        for code in (entry["code"], *entry.get("aliases", [])):
            if code in aliases:
                raise ContractCheckError("duplicate_or_ambiguous_code")
            aliases.add(code)
        if entry.get("failure_class") not in classes or entry.get("retry_policy") not in registry.get("policies", {}):
            raise ContractCheckError("invalid_registry_policy")
        policy = registry["policies"][entry["retry_policy"]]
        if policy.get("class") != entry["failure_class"]:
            raise ContractCheckError("inconsistent_registry_policy")
        if entry["failure_class"] == "needs_user" and entry.get("action_id") not in registry.get("actions", {}):
            raise ContractCheckError("invalid_registry_owner_action")
        if not entry.get("owner") or not entry.get("emitter_scopes") or not entry.get("user_message"):
            raise ContractCheckError("missing_registry_provenance")
        legacy = entry.get("legacy", {})
        if legacy.get("failure_class") not in classes or type(legacy.get("retryable")) is not bool or not isinstance(legacy.get("code"), str):
            raise ContractCheckError("invalid_legacy_adapter")
    if sorted(entry["code"] for entry in registry["codes"]) != schema["properties"]["code"]["enum"]:
        raise ContractCheckError("registry_schema_code_mismatch")
    return registry, aliases


def check_emitters(root=ROOT):
    registry, codes = check_registry(root)
    manifest = json.loads((root / "contracts/failure-emitter-scopes.json").read_text())
    entries = manifest.get("dynamic_adapters", []) + manifest.get("nonfailure_sites", [])
    declarations = {entry["site_sha256"]: entry for entry in entries}
    if len(declarations) != len(entries):
        raise ContractCheckError("duplicate_emitter_audit")
    errors, seen = [], set()
    rows = inventory(root)
    allowed_adapters = {"authority_registry_boundary", "validated_v1_projection", "audited_v0_observation", "closed_backend_code_union", "nonfailure_category"}
    for row in rows:
        where = f"{row['path']}:{row['line']} {row['function']}"
        key = site_key(row)
        audit = declarations.get(key)
        if audit is not None:
            seen.add(key)
            if (audit.get("adapter") not in allowed_adapters or not audit.get("owner") or not audit.get("test")
                    or not audit.get("source") or any(audit.get(field) != row[field] for field in ("path", "function", "field", "expression"))
                    or audit.get("allowed_codes_ref") != "contracts/failure-codes.json#/codes"):
                errors.append(f"{where}: invalid exact emitter audit")
            elif not audit["source"].strip() or any("*" in str(audit.get(field, "")) for field in ("path", "function", "source")):
                errors.append(f"{where}: forbidden wildcard emitter audit")
            if row["literals"] is not None and audit.get("adapter") != "nonfailure_category":
                unknown = set(row["literals"]) - codes - {""}
                if unknown:
                    errors.append(f"{where}: unknown literal in bounded adapter")
        elif row["literals"] is None:
            errors.append(f"{where}: unaudited dynamic {row['field']} emitter")
        else:
            for code in row["literals"]:
                if code and code not in codes:
                    errors.append(f"{where}: unregistered {row['field']} literal")
    for key in declarations.keys() - seen:
        errors.append(f"{declarations[key]['path']} {declarations[key]['function']}: stale emitter audit")
    return errors, rows


def verify_vendor(root=ROOT, *, remote=False, token=None, fetch=None):
    """Verify exact local digests and optionally the pinned private source bytes."""
    lock = json.loads((root / "contracts/failure-codes.lock.json").read_text())
    if type(lock.get("schema_version")) is not int or lock["schema_version"] != 1:
        raise ContractCheckError("invalid_vendor_lock_version")
    revision = lock.get("source_revision")
    if lock.get("source_repository") != SOURCE_REPOSITORY or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ContractCheckError("source_revision_must_be_immutable_commit")
    if set(lock.get("files", {})) != set(ARTIFACTS):
        raise ContractCheckError("invalid_vendor_file_set")
    if remote and not token:
        raise ContractCheckError("CONTENT_FACTORY_CONTRACT_READ_TOKEN_required_for_pinned_private_source")
    for path in ARTIFACTS:
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != lock["files"][path]:
            raise ContractCheckError("vendor_digest_mismatch")
        if remote:
            peer = fetch(path, revision, token) if fetch else fetch_source(path, revision, token)
            if peer != raw:
                raise ContractCheckError("pinned_source_byte_mismatch")
    return lock


def fetch_source(path, revision, token):
    request = Request(f"https://api.github.com/repos/{SOURCE_REPOSITORY}/contents/{path}?ref={revision}",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(request, timeout=20) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ContractCheckError("pinned_source_response_oversized")
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise ContractCheckError("pinned_source_response_invalid")
        return base64.b64decode(payload["content"].replace("\n", ""), validate=True)
    except HTTPError as exc:
        raise ContractCheckError(f"pinned_source_access_failed_http_{exc.code}") from None
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        raise ContractCheckError("pinned_source_fetch_failed") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    if args.inventory:
        print(json.dumps(inventory(), sort_keys=True, indent=2)); return 0
    try:
        errors, rows = check_emitters()
        verify_vendor(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
        if errors:
            print("\n".join(errors)); return 1
    except (ContractCheckError, OSError, ValueError) as exc:
        print(f"Failure contract check failed: {exc}"); return 1
    print(f"Failure contract: {len(rows)} production emissions audited; vendored digests valid.")
    print("Pinned Content Factory provenance verified." if args.remote else "OFFLINE: remote pinned provenance was not rechecked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
