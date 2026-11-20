#!/usr/bin/env python3
"""Audit vendored B1 bytes, exact emitters and immutable CF provenance.

Offline validation is explicit about its limit. CI must use --remote; unavailable
private-source access fails without printing tokens, response bodies or URLs.
Receivers validate failure codes by membership, so a difference from the pinned
Content Factory copy is reported as drift (codes and classes), not a failure.
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
CONSTRUCTORS = {"WebsiteAuthorityError": 0, "RunCommandConflict": 0}
# P2 (IF-P2-27): a package's own emitter-audit fragment may hold only these keys.
FRAGMENT_DIRECTORY = "contracts/failure-emitter-scopes.d"
FRAGMENT_KEYS = frozenset({"dynamic_adapters", "nonfailure_sites"})
# P2 (IF-P2-01, IF-P2-03): registry v2 vocabulary and the pre-P2 overlay.
LIMIT_KEYS = frozenset({"repairable_max_attempts", "strategy_max_attempts_per_rung_per_source",
                        "strategy_max_attempts_per_org_per_day", "wall_hours", "pull_requests_per_org_per_day",
                        "source_repins_per_run"})
NO_CHARGE_ACTIONS = frozenset({"buy_points", "agree_terms"})
OVERLAY_PATH = "content_factory/failure_registry_pre_p2.json"
PRE_P2_FIXTURE_PATH = "content_factory/tests/fixtures/p2/failure-codes.pre-p2.json"
OVERLAY_RESERVED_KEYS = frozenset({"actions", "source_revision"})
OVERLAY_FIELDS = ("failure_class", "retry_policy", "action_id")
UNREGISTERED = "UNREGISTERED_FAILURE_CODE"


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
    errors = registry_v2_errors(registry)
    if errors:
        raise ContractCheckError("; ".join(errors))
    check_overlay(root, registry)
    return registry, aliases


def registry_v2_errors(registry):
    """P2 (IF-P2-01) vocabulary checks over the raw registry; each defect is named."""
    errors = []
    actions = registry.get("actions") if isinstance(registry.get("actions"), dict) else {}
    aliases = registry.get("action_aliases", {})
    retired = registry.get("retired_actions", [])
    copy_ids = registry.get("founder_copy_ids", [])
    if not isinstance(aliases, dict) or any(alias in actions or target not in actions for alias, target in aliases.items()):
        errors.append("invalid_action_aliases")
        aliases = {}
    if (not isinstance(retired, list) or retired != sorted(set(retired)) or
            any(action_id in actions or action_id in aliases for action_id in retired)):
        errors.append("invalid_retired_actions")
        retired = []
    if (not isinstance(copy_ids, list) or not copy_ids or copy_ids != sorted(set(copy_ids)) or
            any(not isinstance(copy_id, str) for copy_id in copy_ids)):
        errors.append("invalid_founder_copy_ids")
        copy_ids = []
    limits = registry.get("limits")
    if (not isinstance(limits, dict) or set(limits) != LIMIT_KEYS or
            any(type(value) is not int or value < 1 for value in limits.values())):
        errors.append("invalid_registry_limits")
    for entry in registry.get("codes", []):
        code = entry.get("code")
        if entry.get("founder_copy_id") not in copy_ids:
            errors.append(f"{code}: missing_or_unknown_founder_copy_id")
        elif entry["founder_copy_id"].startswith("ask_") is not (entry.get("failure_class") == "needs_user"):
            errors.append(f"{code}: founder_copy_id_class_mismatch")
        if type(entry.get("refund")) is not bool:
            errors.append(f"{code}: missing_refund_flag")
        elif entry["refund"] is (entry.get("failure_class") == "needs_user" and entry.get("action_id") in NO_CHARGE_ACTIONS):
            errors.append(f"{code}: invalid_refund_flag")
        if entry.get("failure_class") == "needs_user" and entry.get("action_id") not in actions:
            errors.append(f"{code}: noncanonical_owner_action")
    return errors


def check_overlay(root, registry):
    """The vendored pre-P2 overlay restores exactly today's view (IF-P2-03).

    Every code whose class, policy or action changed against the vendored
    pre-P2 fixture has an entry holding today's values. A new code has an entry
    only when a pre-P2 path already emits it, and then it holds the unregistered
    stop every reader saw before P2. Nothing else is overlaid.
    """
    try:
        overlay = json.loads((root / OVERLAY_PATH).read_text())
        pre = json.loads((root / PRE_P2_FIXTURE_PATH).read_text())
    except (OSError, ValueError):
        raise ContractCheckError("missing_or_unreadable_pre_p2_overlay") from None
    if not isinstance(overlay, dict) or overlay.get("actions") != pre.get("actions"):
        raise ContractCheckError("stale_pre_p2_overlay")
    before = {entry["code"]: entry for entry in pre["codes"]}
    current = {entry["code"]: entry for entry in registry["codes"]}
    expected = {}
    for name, entry in current.items():
        old = before.get(name)
        if old is None:
            continue
        if any(old.get(field) != entry.get(field) for field in OVERLAY_FIELDS):
            expected[name] = old
    for name, values in overlay.items():
        if name in OVERLAY_RESERVED_KEYS:
            continue
        if name not in current or not isinstance(values, dict):
            raise ContractCheckError("stale_pre_p2_overlay")
        if name not in before:
            expected[name] = before[UNREGISTERED]
    for name, old in expected.items():
        values = overlay.get(name)
        if (not isinstance(values, dict) or
                values != {"action_id": old.get("action_id"), "failure_class": old["failure_class"],
                           "retry_policy": old["retry_policy"], "retryable": old["failure_class"] == "transient"}):
            raise ContractCheckError("stale_pre_p2_overlay")
    if set(overlay) - OVERLAY_RESERVED_KEYS != set(expected):
        raise ContractCheckError("stale_pre_p2_overlay")


def load_manifest(root=ROOT):
    """The base emitter manifest merged with every package fragment (IF-P2-27).

    Fragments load in sorted file-name order and may hold only `dynamic_adapters`
    and `nonfailure_sites`. An unknown key, a non-list value, a site key another
    file (or the same fragment) already declares, or an audit whose site key no
    longer matches its own fields is an error.
    """
    errors = []
    manifest = json.loads((root / "contracts/failure-emitter-scopes.json").read_text())
    merged = {key: list(manifest.get(key, [])) for key in FRAGMENT_KEYS}
    seen = {entry.get("site_sha256") for key in FRAGMENT_KEYS for entry in merged[key]}
    directory = root / FRAGMENT_DIRECTORY
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        name = f"{FRAGMENT_DIRECTORY}/{path.name}"
        try:
            fragment = json.loads(path.read_text())
        except ValueError:
            errors.append(f"{name}: invalid JSON"); continue
        if not isinstance(fragment, dict):
            errors.append(f"{name}: a fragment must be an object"); continue
        for key in sorted(set(fragment) - FRAGMENT_KEYS):
            errors.append(f"{name}: unknown fragment key {key}")
        for key in sorted(FRAGMENT_KEYS & set(fragment)):
            if not isinstance(fragment[key], list):
                errors.append(f"{name}: {key} must be a list"); continue
            for entry in fragment[key]:
                if not isinstance(entry, dict) or any(not isinstance(entry.get(field), str)
                                                      for field in ("path", "function", "field", "expression", "site_sha256")):
                    errors.append(f"{name}: invalid {key} entry"); continue
                if entry["site_sha256"] in seen:
                    errors.append(f"{name}: duplicate site key {entry['site_sha256']} ({entry['path']} {entry['function']})")
                    continue
                if entry["site_sha256"] != site_key(entry):
                    errors.append(f"{name}: stale audit {entry['path']} {entry['function']} (site key does not match its fields)")
                    continue
                seen.add(entry["site_sha256"])
                merged[key].append(entry)
    manifest.update(merged)
    return manifest, errors


def check_emitters(root=ROOT):
    registry, codes = check_registry(root)
    manifest, errors = load_manifest(root)
    entries = manifest.get("dynamic_adapters", []) + manifest.get("nonfailure_sites", [])
    declarations = {entry["site_sha256"]: entry for entry in entries}
    if len(declarations) != len(entries):
        raise ContractCheckError("duplicate_emitter_audit")
    seen = set()
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


# F4 (plan 7.5): registry rules and ratchets for the backend's side of the
# contract. Content Factory's checker owns rules 1, 2 and 4 (its emitters, its
# exception classes, the canonical registry's owners); this one checks what the
# backend renders and schedules: rule 3 (one founder state; every ask rendered in
# both journeys), rule 5 (operator-only remedies), rule 6 (stop codes) and
# rule 7 (new sweeps and alert rules ship with a shadow flag and a history horizon).
RULES_PATH = "contracts/backend-registry-rules.json"
RULES_KEYS = frozenset({"schema_version", "about", "ratchets", "render_gaps", "operator_only_codes", "sweeps"})
RATCHET_KEYS = frozenset({"stop_codes", "render_gaps", "grandfathered_sweeps"})
STOP_COPY_IDS = frozenset({"stopped", "fixing"})
WORKING_COPY_PREFIXES = ("working", "done_copy_ready")
JOURNEY_PATH = "content_factory/founder_journey.py"
JOURNEY_P2_PATH = "content_factory/founder_journey_p2.py"
LEGACY_STATUS_PATH = "content_factory/website_status.py"
REGISTRY_POLICY_PATH = "content_factory/registry_policy.py"
DEPLOY_VALIDATOR_PATH = "scripts/validate_articles_deploy_config.py"
SETTINGS_PATH = "mlai/settings.py"
SWEEP_SOURCES = {"self_heal": "content_factory/self_heal_sweeps.py", "founder_journey": "content_factory/founder_journey_sweeps.py",
                 "alert_rule": "content_factory/staff_alert_rules.py"}
HORIZON_READERS = {"ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE": "alerts_since("}


def _literal(tree, name, *, function=None):
    """A module-level (or one function's) literal assignment, or the keys of a non-literal dict."""
    scope = tree
    if function is not None:
        scope = next((node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function), None)
        if scope is None:
            return None
    for node in ast.walk(scope):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets) and node.value is not None:
            value = node.value.args[0] if isinstance(node.value, ast.Call) and node.value.args else node.value
            try:
                return ast.literal_eval(value)
            except ValueError:
                if isinstance(value, ast.Dict):
                    return {key.value: None for key in value.keys if isinstance(key, ast.Constant)}
                return None
    return None


def _dict_keys_with(tree, member):
    """Keys of the first dict literal in ``tree`` that has ``member`` as a key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(isinstance(key, ast.Constant) and key.value == member for key in node.keys):
            return {key.value for key in node.keys if isinstance(key, ast.Constant)}
    return set()


def _string_constants(tree):
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _policy_stops(registry, policy):
    value = registry.get("policies", {}).get(policy)
    return isinstance(value, dict) and value.get("mode") == "stop"


def founder_state(registry, entry):
    """Rule 3: ``ask``, ``working`` or ``fixing`` for one registry entry, or (None, reason)."""
    copy_id = entry.get("founder_copy_id")
    if entry.get("failure_class") == "needs_user":
        return ("ask", None) if isinstance(copy_id, str) and copy_id.startswith("ask_") else (None, "an ask needs an ask copy id")
    if _policy_stops(registry, entry.get("retry_policy")):
        if copy_id in STOP_COPY_IDS:
            return "fixing", None
        return None, ("working copy paired with a non-retryable stop" if isinstance(copy_id, str)
                      and copy_id.startswith(WORKING_COPY_PREFIXES) else f"stop with copy {copy_id}")
    if isinstance(copy_id, str) and (copy_id.startswith(WORKING_COPY_PREFIXES) or copy_id == "fixing"):
        return ("fixing" if copy_id == "fixing" else "working"), None
    return None, f"copy {copy_id} on a code the engine still acts on"


def journey_facts(root):
    """What each journey renders, read statically from the backend's own modules."""
    def tree(path):
        return ast.parse((root / path).read_text(), filename=path)
    journey, journey_p2, legacy, policy = (tree(path) for path in (JOURNEY_PATH, JOURNEY_P2_PATH, LEGACY_STATUS_PATH,
                                                                    REGISTRY_POLICY_PATH))
    legacy_actions = _literal(legacy, "OWNER_ACTIONS") or set()
    legacy_labels = _literal(legacy, "labels", function="_action") or {}
    return {"founder_asks": set(_literal(journey, "FOUNDER_ASKS") or ()),
            "retired": set(_literal(journey, "RETIRED_ACTIONS") or ()),
            "ask_copy": _dict_keys_with(journey_p2, "ask_review_draft"),
            # An ask's copy row is in founder_journey_p2's ask table or, for the
            # P1 path, founder_journey's COPY under the bare action id.
            "copy_strings": _string_constants(journey_p2) | {f"ask_{key}" for key in _literal(journey, "COPY") or {}},
            "legacy_wire": _literal(policy, "LEGACY_WIRE_ACTION_IDS") or {},
            "legacy_rendered": set(legacy_actions) & set(legacy_labels)}


def ask_actions(registry, overlay):
    """Every action a founder can be asked for in either registry view, aliases resolved."""
    aliases = registry.get("action_aliases", {})
    actions = {}
    for entry in registry.get("codes", []):
        if entry.get("failure_class") == "needs_user" and entry.get("action_id"):
            actions.setdefault(aliases.get(entry["action_id"], entry["action_id"]), set()).add(entry["code"])
    for code, values in (overlay or {}).items():
        if isinstance(values, dict) and values.get("failure_class") == "needs_user" and values.get("action_id"):
            actions.setdefault(aliases.get(values["action_id"], values["action_id"]), set()).add(code)
    return actions


def render_gaps(registry, overlay, facts):
    """Rule 3: (journey, action) pairs where a founder ask has no rendered action."""
    gaps = set()
    for action in ask_actions(registry, overlay):
        if action not in facts["founder_asks"] and action not in facts["retired"]:
            gaps.add(("new_journey", action))
        elif (action in facts["founder_asks"] and action not in facts["retired"]
              and f"ask_{action}" not in facts["ask_copy"] | facts["copy_strings"]):
            gaps.add(("new_journey_copy", action))
        wire = facts["legacy_wire"].get(action, action)
        # A None wire id travels without an id: old builds and mobile render the fallback card.
        if wire is not None and wire not in facts["legacy_rendered"]:
            gaps.add(("legacy_journey", action))
    return gaps


def founder_state_errors(registry, facts):
    errors = []
    for entry in registry.get("codes", []):
        state, problem = founder_state(registry, entry)
        if state is None:
            errors.append(f"{entry.get('code')}: no single founder state ({problem})")
        copy_id = entry.get("founder_copy_id")
        if isinstance(copy_id, str) and copy_id not in facts["copy_strings"]:
            errors.append(f"{entry.get('code')}: founder copy {copy_id} is not rendered by {JOURNEY_P2_PATH}")
    return errors


def render_gap_errors(registry, overlay, facts, declared, ratchet):
    errors = []
    found = render_gaps(registry, overlay, facts)
    listed = set()
    for entry in declared if isinstance(declared, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or len(entry["reason"].strip()) < 20:
            errors.append(f"{RULES_PATH}: a render_gaps entry needs journey, action and a reason"); continue
        key = (entry.get("journey"), entry.get("action"))
        listed.add(key)
        if key not in found:
            errors.append(f"{RULES_PATH}: stale render gap {key[0]} {key[1]} (it is rendered now)")
    for journey, action in sorted(found - listed):
        errors.append(f"ask {action}: not rendered in the {journey.replace('_', ' ')} (founder_journey FOUNDER_ASKS, "
                      f"founder_journey_p2 ask copy, website_status OWNER_ACTIONS and labels)")
    errors.extend(ratchet_count_errors("render_gaps", len(found & listed), ratchet))
    return errors


def operator_only_errors(registry, overlay, codes):
    """Rule 5: a code only staff can repair is never a founder ask in either view."""
    errors = []
    by_code = {entry["code"]: entry for entry in registry.get("codes", [])}
    for code in codes if isinstance(codes, list) else []:
        if code not in by_code:
            errors.append(f"{RULES_PATH}: stale operator-only code {code}"); continue
        for view, values in (("v2", by_code[code]), ("pre_p2", (overlay or {}).get(code))):
            if isinstance(values, dict) and values.get("failure_class") == "needs_user":
                errors.append(f"{code}: an operator-only remedy is a founder ask in the {view} view")
    return errors


def stop_code_count(registry):
    """Rule 6: codes whose policy stops."""
    return sum(1 for entry in registry.get("codes", []) if _policy_stops(registry, entry.get("retry_policy")))


def ratchet_count_errors(name, current, ratchet):
    if type(ratchet) is not int or ratchet < 0:
        return [f"{RULES_PATH}: ratchets.{name} must be a non-negative integer"]
    if current > ratchet:
        return [f"{name}: {current} exceeds the ratchet of {ratchet}; it may only fall"]
    if current < ratchet:
        return [f"{name} ratchet: only {current} remain; lower ratchets.{name} from {ratchet} to {current}"]
    return []


def scheduled_sweeps(root):
    """Rule 7: every scheduled sweep and global alert rule, by registry name."""
    names = set()
    for kind, path in SWEEP_SOURCES.items():
        tree = ast.parse((root / path).read_text(), filename=path)
        if kind == "self_heal":
            names |= {f"self_heal:{row[0]}" for row in _literal(tree, "SWEEPS") or ()}
        elif kind == "founder_journey":
            names |= {f"founder_journey:{row}" for row in _literal(tree, "SWEEPS") or ()}
        else:
            names |= {f"alert_rule:{name}" for name in _literal(tree, "GLOBAL_RULES") or {}}
    return names


def _sweep_module(root, name):
    kind, _, rest = name.partition(":")
    if kind == "self_heal":
        tree = ast.parse((root / SWEEP_SOURCES["self_heal"]).read_text())
        module = next((row[1] for row in _literal(tree, "SWEEPS") or () if row[0] == rest), None)
    elif kind == "founder_journey":
        module = rest.partition(":")[0]
    else:
        return root / SWEEP_SOURCES["alert_rule"]
    return root / (module.replace(".", "/") + ".py") if module else None


def sweep_errors(root, declared, ratchet):
    """Rule 7: a sweep or alert rule not grandfathered names a shadow flag and a history horizon.

    Both must be settings a release can install (the deploy config validator's
    keys) and defined in mlai/settings.py, and the sweep's module must read them.
    """
    errors = []
    if not isinstance(declared, dict):
        return [f"{RULES_PATH}: sweeps must be an object"]
    validator = ast.parse((root / DEPLOY_VALIDATOR_PATH).read_text())
    flag_keys = set(_literal(validator, "FLAG_KEYS") or ())
    release_keys = flag_keys | {node.value for node in ast.walk(validator) if isinstance(node, ast.Constant)
                                and isinstance(node.value, str) and node.value.endswith("_SINCE")}
    settings_text = (root / SETTINGS_PATH).read_text()
    found = scheduled_sweeps(root)
    grandfathered = 0
    for name in sorted(found - set(declared)):
        errors.append(f"{name}: a new sweep or alert rule must ship with a shadow flag and a history horizon "
                      f"({RULES_PATH} sweeps)")
    for name, entry in sorted(declared.items()):
        if name not in found:
            errors.append(f"{RULES_PATH}: stale sweep {name}"); continue
        if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or len(entry["reason"].strip()) < 20:
            errors.append(f"{name}: a sweep entry needs a reason"); continue
        if entry.get("grandfathered") is True:
            grandfathered += 1
            continue
        flag, horizon = entry.get("shadow_flag"), entry.get("history_horizon")
        module = _sweep_module(root, name)
        source = module.read_text() if module is not None and module.is_file() else ""
        if not isinstance(flag, str) or flag not in flag_keys or f"{flag} = " not in settings_text:
            errors.append(f"{name}: shadow_flag must be a release-installable flag ({DEPLOY_VALIDATOR_PATH} FLAG_KEYS) "
                          f"defined in {SETTINGS_PATH}")
        elif flag not in source:
            errors.append(f"{name}: the sweep's module does not read its shadow flag {flag}")
        if not isinstance(horizon, str) or horizon not in release_keys or f"{horizon} = " not in settings_text:
            errors.append(f"{name}: history_horizon must be a release-installable *_SINCE setting defined in {SETTINGS_PATH}")
        elif horizon not in source and HORIZON_READERS.get(horizon, "\0") not in source:
            errors.append(f"{name}: the sweep's module does not read its history horizon {horizon}")
    errors.extend(ratchet_count_errors("grandfathered_sweeps", grandfathered, ratchet))
    return errors


def ratchet_regressions(current, base):
    """Ratchets only fall: a value above the base revision's value is an error."""
    return [f"ratchet {name} rose from {base[name]} to {value}; ratchets may only fall"
            for name, value in sorted(current.items())
            if type(value) is int and type(base.get(name)) is int and value > base[name]]


def base_ratchets(root, ref):
    """``ratchets`` of the rules file at git revision ``ref``; an unknown revision is an error."""
    import subprocess
    verify = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            capture_output=True, text=True)
    if verify.returncode:
        raise ContractCheckError("ratchet_base_unknown_revision")
    shown = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{RULES_PATH}"], capture_output=True, text=True)
    if shown.returncode:
        return {}
    value = json.loads(shown.stdout).get("ratchets")
    return value if isinstance(value, dict) else {}


def registry_rule_errors(root=ROOT, registry=None):
    """F4 rules 3, 5, 6 and 7 over ``root``."""
    rules_path = root / RULES_PATH
    if not rules_path.is_file():
        return [f"{RULES_PATH}: missing; the F4 registry rules need it"]
    rules = json.loads(rules_path.read_text())
    if not isinstance(rules, dict) or set(rules) != RULES_KEYS:
        return [f"{RULES_PATH}: must hold exactly {', '.join(sorted(RULES_KEYS))}"]
    ratchets = rules["ratchets"]
    if not isinstance(ratchets, dict) or set(ratchets) != RATCHET_KEYS:
        return [f"{RULES_PATH}: ratchets must hold exactly {', '.join(sorted(RATCHET_KEYS))}"]
    registry = registry if registry is not None else json.loads((root / ARTIFACTS[0]).read_text())
    overlay = json.loads((root / OVERLAY_PATH).read_text())
    facts = journey_facts(root)
    errors = founder_state_errors(registry, facts)
    errors.extend(render_gap_errors(registry, overlay, facts, rules["render_gaps"], ratchets["render_gaps"]))
    errors.extend(operator_only_errors(registry, overlay, rules["operator_only_codes"]))
    errors.extend(ratchet_count_errors("stop_codes", stop_code_count(registry), ratchets["stop_codes"]))
    errors.extend(sweep_errors(root, rules["sweeps"], ratchets["grandfathered_sweeps"]))
    return errors


def describe_drift(path, local, peer):
    """Report how the vendored copy differs from the pinned Content Factory copy."""
    if path != ARTIFACTS[0]:
        return [f"DRIFT {path}: bytes differ from the pinned Content Factory revision"]
    try:
        ours, theirs = ({entry["code"]: entry["failure_class"] for entry in json.loads(raw)["codes"]} for raw in (local, peer))
    except (ValueError, KeyError, TypeError):
        return [f"DRIFT {path}: the pinned Content Factory copy is not a readable registry"]
    def safe(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", value) else "<invalid>"
    lines = [f"DRIFT {path}: only in Content Factory: {safe(code)}" for code in sorted(theirs.keys() - ours.keys(), key=str)]
    lines += [f"DRIFT {path}: only here: {code}" for code in sorted(ours.keys() - theirs.keys())]
    lines += [f"DRIFT {path}: {code} is {ours[code]} here and {safe(theirs[code])} in Content Factory"
              for code in sorted(ours.keys() & theirs.keys()) if ours[code] != theirs[code]]
    return lines or [f"DRIFT {path}: bytes differ; codes and classes agree"]


def verify_vendor(root=ROOT, *, remote=False, token=None, fetch=None):
    """Verify exact local digests; with remote, return drift from the pinned source.

    Local digests, the immutable pin and private-source access still fail
    closed. Byte differences from the pinned Content Factory copy are returned
    as drift lines because receivers accept codes by membership.
    """
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
    drift = []
    for path in ARTIFACTS:
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != lock["files"][path]:
            raise ContractCheckError("vendor_digest_mismatch")
        if remote:
            peer = fetch(path, revision, token) if fetch else fetch_source(path, revision, token)
            if peer != raw:
                drift.extend(describe_drift(path, raw, peer))
    return drift


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
    parser.add_argument("--ratchet-base", help="git revision whose F4 ratchets this tree may not exceed")
    args = parser.parse_args()
    if args.inventory:
        print(json.dumps(inventory(), sort_keys=True, indent=2)); return 0
    try:
        errors, rows = check_emitters()
        errors.extend(registry_rule_errors())
        if args.ratchet_base:
            rules = json.loads((ROOT / RULES_PATH).read_text())
            errors.extend(ratchet_regressions(rules.get("ratchets", {}), base_ratchets(ROOT, args.ratchet_base)))
        drift = verify_vendor(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
        if errors:
            print("\n".join(errors)); return 1
    except (ContractCheckError, OSError, ValueError) as exc:
        print(f"Failure contract check failed: {exc}"); return 1
    print(f"Failure contract: {len(rows)} production emissions audited; vendored digests valid.")
    if drift:
        print("\n".join(drift))
        print("Pinned Content Factory copy differs (reported, not a gate); bump source_revision once both repos agree.")
    else:
        print("Pinned Content Factory provenance verified." if args.remote else "OFFLINE: remote pinned provenance was not rechecked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
