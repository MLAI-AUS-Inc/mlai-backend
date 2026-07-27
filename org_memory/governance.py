from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


SUPPORTED_PROVIDERS = frozenset(
    {
        "google_drive",
        "slack",
        "linear",
        "notion",
        "gmail",
        "stripe",
        "xero",
        "luma",
    }
)

REQUIRED_OWNER_ROLES = ("data", "security", "review", "operations")
REQUIRED_RETENTION_FIELDS = (
    "raw_evidence_days",
    "derived_memory_days",
    "query_audit_days",
)
REQUIRED_COST_FIELDS = ("daily_model_budget_aud", "monthly_model_budget_aud")
APPROVED_STATUS = "approved"

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_POLICY_PATH = PACKAGE_ROOT / "policies" / "provider_policies.json"
DEFAULT_EVAL_FIXTURE_PATH = PACKAGE_ROOT / "evals" / "seed_cases.jsonl"


class GovernancePolicyError(RuntimeError):
    """Raised when a provider is used outside its approved governance policy."""


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def parse_enabled_providers(raw: str | Iterable[str] | None) -> set[str]:
    """Normalise comma/space separated configuration into provider identifiers."""

    if raw is None:
        return set()
    if isinstance(raw, str):
        values = raw.replace(",", " ").split()
    else:
        values = [str(value) for value in raw]
    return {value.strip().lower() for value in values if value.strip()}


def configured_enabled_providers() -> set[str]:
    return parse_enabled_providers(os.getenv("ORG_MEMORY_ENABLED_PROVIDERS", ""))


def load_policy_manifest(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GovernancePolicyError(f"Governance manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise GovernancePolicyError(
            f"Governance manifest is not valid JSON at line {exc.lineno}: {manifest_path}"
        ) from exc

    if not isinstance(payload, dict):
        raise GovernancePolicyError("Governance manifest root must be a JSON object.")
    return payload


def _validate_manifest_structure(manifest: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []

    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1.")
    if not _is_non_empty_string(manifest.get("policy_id")):
        errors.append("policy_id is required.")
    if manifest.get("default_production_decision") != "deny":
        errors.append("default_production_decision must be 'deny'.")

    providers = _as_mapping(manifest.get("providers"))
    provider_names = set(providers)
    missing = SUPPORTED_PROVIDERS - provider_names
    unknown = provider_names - SUPPORTED_PROVIDERS
    if missing:
        errors.append(f"provider policies are missing: {', '.join(sorted(missing))}.")
    if unknown:
        errors.append(f"unknown provider policies are present: {', '.join(sorted(unknown))}.")

    for provider_name in sorted(SUPPORTED_PROVIDERS & provider_names):
        policy = _as_mapping(providers.get(provider_name))
        prefix = f"providers.{provider_name}"
        if policy.get("provider") != provider_name:
            errors.append(f"{prefix}.provider must equal '{provider_name}'.")
        if not isinstance(policy.get("production_enabled"), bool):
            errors.append(f"{prefix}.production_enabled must be a boolean.")
        if policy.get("classification") not in {
            "internal",
            "committee",
            "executive",
            "finance",
            "people_sensitive",
            "no_agent",
        }:
            errors.append(f"{prefix}.classification is invalid.")
        if not isinstance(policy.get("allowed_memory_types"), list):
            errors.append(f"{prefix}.allowed_memory_types must be a list.")
        if not isinstance(policy.get("source_scope"), Mapping):
            errors.append(f"{prefix}.source_scope must be an object.")
        if not isinstance(policy.get("retention"), Mapping):
            errors.append(f"{prefix}.retention must be an object.")
        approval = _as_mapping(policy.get("approval"))
        if approval.get("status") not in {"draft", "approved", "suspended", "rejected"}:
            errors.append(f"{prefix}.approval.status is invalid.")

    slack = _as_mapping(providers.get("slack"))
    slack_scope = _as_mapping(slack.get("source_scope"))
    if slack_scope.get("direct_messages") != "excluded":
        errors.append("providers.slack.source_scope.direct_messages must be 'excluded' for the MVP.")

    stripe = _as_mapping(providers.get("stripe"))
    xero = _as_mapping(providers.get("xero"))
    if stripe.get("aggregate_only") is not True:
        errors.append("providers.stripe.aggregate_only must be true.")
    if xero.get("aggregate_only") is not True:
        errors.append("providers.xero.aggregate_only must be true.")

    luma = _as_mapping(providers.get("luma"))
    if luma.get("attendee_pii_allowed") is not False:
        errors.append("providers.luma.attendee_pii_allowed must be false.")

    drive = _as_mapping(providers.get("google_drive"))
    drive_inventory = _as_mapping(drive.get("inventory"))
    if drive_inventory.get("status") not in {"draft", "approved", "suspended", "rejected"}:
        errors.append("providers.google_drive.inventory.status is invalid.")

    return errors


def _validate_production_provider(
    manifest: Mapping[str, Any], provider_name: str
) -> list[str]:
    errors: list[str] = []
    providers = _as_mapping(manifest.get("providers"))
    policy = _as_mapping(providers.get(provider_name))
    prefix = f"providers.{provider_name}"

    if not policy:
        return [f"{prefix} has no policy record."]
    if manifest.get("status") != APPROVED_STATUS:
        errors.append("manifest status must be 'approved' for production.")
    if not _is_non_empty_string(manifest.get("last_reviewed_at")):
        errors.append("last_reviewed_at is required for production.")
    if policy.get("production_enabled") is not True:
        errors.append(f"{prefix}.production_enabled is not true.")

    approval = _as_mapping(policy.get("approval"))
    if approval.get("status") != APPROVED_STATUS:
        errors.append(f"{prefix}.approval.status is not 'approved'.")
    for field in ("approved_by", "approved_at", "terms_reviewed_by", "terms_reviewed_at"):
        if not _is_non_empty_string(approval.get(field)):
            errors.append(f"{prefix}.approval.{field} is required for production.")

    owners = _as_mapping(manifest.get("owners"))
    for owner_role in REQUIRED_OWNER_ROLES:
        if not _is_non_empty_string(owners.get(owner_role)):
            errors.append(f"owners.{owner_role} is required for production.")

    global_rules = _as_mapping(manifest.get("global_rules"))
    if global_rules.get("hard_delete_rules_approved") is not True:
        errors.append("global_rules.hard_delete_rules_approved must be true for production.")

    if not _is_non_empty_string(policy.get("review_owner")):
        errors.append(f"{prefix}.review_owner is required for production.")

    source_scope = _as_mapping(policy.get("source_scope"))
    selectors = source_scope.get("selectors")
    if not isinstance(selectors, list) or not selectors:
        errors.append(f"{prefix}.source_scope.selectors must contain an approved scope.")

    authority = _as_mapping(policy.get("authority"))
    if not _is_non_empty_string(authority.get("default")):
        errors.append(f"{prefix}.authority.default is required for production.")
    if not isinstance(policy.get("allowed_memory_types"), list) or not policy.get(
        "allowed_memory_types"
    ):
        errors.append(f"{prefix}.allowed_memory_types must not be empty in production.")

    retention = _as_mapping(policy.get("retention"))
    for field in REQUIRED_RETENTION_FIELDS:
        if not _is_positive_number(retention.get(field)):
            errors.append(f"{prefix}.retention.{field} must be a positive number.")

    slos = _as_mapping(manifest.get("slos"))
    if slos.get("approval_status") != APPROVED_STATUS:
        errors.append("slos.approval_status must be 'approved' for production.")
    if not _is_non_empty_string(slos.get("approved_by")):
        errors.append("slos.approved_by is required for production.")
    cost_limits = _as_mapping(slos.get("cost_limits"))
    for field in REQUIRED_COST_FIELDS:
        if not _is_positive_number(cost_limits.get(field)):
            errors.append(f"slos.cost_limits.{field} must be a positive number.")

    if provider_name == "slack":
        if source_scope.get("direct_messages") != "excluded":
            errors.append("Slack direct messages must remain excluded from ingestion.")
        if not _is_non_empty_string(source_scope.get("acquisition_method")):
            errors.append("providers.slack.source_scope.acquisition_method is required.")

    return errors


def validate_policy_manifest(
    manifest: Mapping[str, Any],
    *,
    enabled_providers: Iterable[str] = (),
    production: bool = False,
) -> list[str]:
    """Return all structural and production-gate errors for a manifest."""

    errors = _validate_manifest_structure(manifest)
    explicitly_enabled = parse_enabled_providers(enabled_providers)
    manifest_enabled = {
        name
        for name, policy in _as_mapping(manifest.get("providers")).items()
        if isinstance(policy, Mapping) and policy.get("production_enabled") is True
    }
    requested = explicitly_enabled | manifest_enabled

    unknown = requested - SUPPORTED_PROVIDERS
    if unknown:
        errors.append(f"enabled providers are unsupported: {', '.join(sorted(unknown))}.")

    if production:
        for provider_name in sorted(requested & SUPPORTED_PROVIDERS):
            errors.extend(_validate_production_provider(manifest, provider_name))

    return errors


def assert_provider_ingestion_allowed(
    provider_name: str,
    *,
    manifest: Mapping[str, Any] | None = None,
    production: bool,
) -> Mapping[str, Any]:
    """Fail closed before a future connector starts production ingestion.

    Inventory and fixture work may run outside production against draft policies.
    Every production connector entry point must call this guard before fetching
    source content.
    """

    provider_name = str(provider_name).strip().lower()
    active_manifest = manifest if manifest is not None else load_policy_manifest()
    errors = validate_policy_manifest(
        active_manifest,
        enabled_providers={provider_name},
        production=production,
    )
    if provider_name not in SUPPORTED_PROVIDERS:
        errors.append(f"Provider '{provider_name}' is unsupported.")

    if errors:
        joined = "\n- ".join(errors)
        raise GovernancePolicyError(
            f"Organisational-memory ingestion is denied for '{provider_name}':\n- {joined}"
        )

    return _as_mapping(_as_mapping(active_manifest.get("providers")).get(provider_name))


def assert_provider_inventory_allowed(
    provider_name: str,
    selectors: Iterable[str],
    *,
    requested_max_files: int,
    manifest: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Authorise a metadata-only inventory without enabling content ingestion."""

    provider_name = str(provider_name).strip().lower()
    active_manifest = manifest if manifest is not None else load_policy_manifest()
    errors = validate_policy_manifest(active_manifest)
    providers = _as_mapping(active_manifest.get("providers"))
    policy = _as_mapping(providers.get(provider_name))

    requested_selectors = {str(value).strip() for value in selectors if str(value).strip()}
    if provider_name not in SUPPORTED_PROVIDERS or not policy:
        errors.append(f"Provider '{provider_name}' has no supported inventory policy.")
    else:
        inventory = _as_mapping(policy.get("inventory"))
        if inventory.get("status") != APPROVED_STATUS:
            errors.append(f"providers.{provider_name}.inventory.status is not 'approved'.")
        for field in ("approved_by", "approved_at", "privacy_approved_by"):
            if not _is_non_empty_string(inventory.get(field)):
                errors.append(f"providers.{provider_name}.inventory.{field} is required.")

        approved_max_files = inventory.get("max_files")
        if (
            isinstance(approved_max_files, bool)
            or not isinstance(approved_max_files, int)
            or approved_max_files <= 0
        ):
            errors.append(
                f"providers.{provider_name}.inventory.max_files must be a positive integer."
            )
        elif (
            isinstance(requested_max_files, bool)
            or not isinstance(requested_max_files, int)
            or requested_max_files <= 0
        ):
            errors.append("requested_max_files must be a positive integer.")
        elif requested_max_files > approved_max_files:
            errors.append(
                f"Requested max_files {requested_max_files} exceeds the approved ceiling "
                f"{approved_max_files}."
            )

        raw_approved_selectors = _as_mapping(policy.get("source_scope")).get("selectors")
        approved_selectors = (
            {
                str(value).strip()
                for value in raw_approved_selectors
                if str(value).strip()
            }
            if isinstance(raw_approved_selectors, list)
            else set()
        )
        unapproved = requested_selectors - approved_selectors
        if not requested_selectors:
            errors.append("At least one inventory selector is required.")
        if unapproved:
            errors.append(
                "Inventory selectors are not approved: " + ", ".join(sorted(unapproved)) + "."
            )

        owners = _as_mapping(active_manifest.get("owners"))
        for owner_role in ("data", "security"):
            if not _is_non_empty_string(owners.get(owner_role)):
                errors.append(f"owners.{owner_role} is required for inventory.")

    if errors:
        joined = "\n- ".join(errors)
        raise GovernancePolicyError(
            f"Organisational-memory inventory is denied for '{provider_name}':\n- {joined}"
        )

    return policy


def load_seed_evaluations(path: str | Path = DEFAULT_EVAL_FIXTURE_PATH) -> list[dict[str, Any]]:
    fixture_path = Path(path)
    cases: list[dict[str, Any]] = []
    try:
        lines = fixture_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise GovernancePolicyError(f"Seed evaluation fixture does not exist: {fixture_path}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GovernancePolicyError(
                f"Seed evaluation fixture has invalid JSON on line {line_number}."
            ) from exc
        if not isinstance(case, dict):
            raise GovernancePolicyError(
                f"Seed evaluation fixture line {line_number} must be an object."
            )
        cases.append(case)
    return cases


def validate_seed_evaluations(cases: Iterable[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    allowed_outcomes = {"answer", "deny", "abstain"}

    for index, case in enumerate(cases, start=1):
        prefix = f"case[{index}]"
        case_id = case.get("id")
        if not _is_non_empty_string(case_id):
            errors.append(f"{prefix}.id is required.")
        elif case_id in seen_ids:
            errors.append(f"{prefix}.id '{case_id}' is duplicated.")
        else:
            seen_ids.add(str(case_id))

        if not _is_non_empty_string(case.get("category")):
            errors.append(f"{prefix}.category is required.")
        if not _is_non_empty_string(case.get("question")):
            errors.append(f"{prefix}.question is required.")
        if not isinstance(case.get("actor"), Mapping):
            errors.append(f"{prefix}.actor must be an object.")
        evidence_items = case.get("evidence")
        if not isinstance(evidence_items, list):
            errors.append(f"{prefix}.evidence must be a list.")
            evidence_items = []

        expected = _as_mapping(case.get("expected"))
        if expected.get("outcome") not in allowed_outcomes:
            errors.append(f"{prefix}.expected.outcome is invalid.")
        for field in (
            "required_evidence_ids",
            "forbidden_evidence_ids",
            "must_contain",
            "must_not_contain",
        ):
            if not isinstance(expected.get(field), list):
                errors.append(f"{prefix}.expected.{field} must be a list.")

        evidence_ids = {
            item.get("id")
            for item in evidence_items
            if isinstance(item, Mapping) and _is_non_empty_string(item.get("id"))
        }
        required_ids = expected.get("required_evidence_ids")
        forbidden_ids = expected.get("forbidden_evidence_ids")
        expected_ids = set(required_ids if isinstance(required_ids, list) else []) | set(
            forbidden_ids if isinstance(forbidden_ids, list) else []
        )
        missing_evidence = expected_ids - evidence_ids
        if missing_evidence:
            errors.append(
                f"{prefix} references unknown evidence IDs: {', '.join(sorted(missing_evidence))}."
            )

    return errors
