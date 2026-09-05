import hashlib
import hmac
import json
import re
from collections import defaultdict

from django.conf import settings
from django.db import migrations


INTERNAL_ACTOR_PATTERNS = (
    re.compile(r"mlai_user:([1-9][0-9]*)\Z"),
    re.compile(r"web_([1-9][0-9]*)\Z"),
)
INTEGRATION_STATE_FIELDS = (
    "github_access_token",
    "github_refresh_token",
    "github_token_expires_at",
    "github_user_name",
    "github_repo",
    "github_scopes",
    "github_installation_id",
    "project_scanned",
    "last_scanned_sha",
    "last_scanned_at",
    "pending_intent",
)
RECOVERY_RUNBOOK = "docs/slack-founder-actor-migration-recovery.md"
GUARD_VERSION = "orphaned-internal-actor-attestation-v1"


def _internal_actor_user_id(value):
    normalized = str(value or "").strip()
    for pattern in INTERNAL_ACTOR_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match:
            return int(match.group(1))
    return None


def _internal_actor_ids_in_payload(value):
    actor_ids = set()
    if isinstance(value, str):
        if _internal_actor_user_id(value) is not None:
            actor_ids.add(value)
    elif isinstance(value, list):
        for item in value:
            actor_ids.update(_internal_actor_ids_in_payload(item))
    elif isinstance(value, dict):
        for item in value.values():
            actor_ids.update(_internal_actor_ids_in_payload(item))
    return actor_ids


def _stable_digest(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _attestation_fingerprint(findings, state_fingerprints):
    payload = {
        "guard_version": GUARD_VERSION,
        "orphaned_internal_actor_history": sorted(findings),
        "orphaned_internal_actor_state": sorted(state_fingerprints),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def guard_orphaned_actor_migration_history(apps, schema_editor):
    User = apps.get_model("core", "User")
    OrganizationContentConfig = apps.get_model(
        "content_factory", "OrganizationContentConfig"
    )
    ContentFactoryJob = apps.get_model("content_factory", "ContentFactoryJob")
    ScheduledDiscoveryDispatch = apps.get_model(
        "content_factory", "ScheduledDiscoveryDispatch"
    )
    ContentFactoryRun = apps.get_model("workflow_runs", "ContentFactoryRun")
    UserIntegration = apps.get_model("integrations", "UserIntegration")

    existing_user_ids = set(User.objects.values_list("pk", flat=True).iterator())
    orphan_sources = defaultdict(set)
    state_fingerprints = set()

    def record_actor(actor_id, source, state):
        user_id = _internal_actor_user_id(actor_id)
        if user_id is None or user_id in existing_user_ids:
            return False
        orphan_sources[actor_id].add(source)
        state_fingerprints.add(f"{source}:{_stable_digest(state)}")
        return True

    integration_fields = ("slack_user_id",) + INTEGRATION_STATE_FIELDS
    integrations = UserIntegration.objects.only(*integration_fields)
    for integration in integrations.iterator(chunk_size=500):
        source = f"integrations.UserIntegration.slack_user_id#{integration.pk}"
        state = tuple(
            getattr(integration, field_name) for field_name in INTEGRATION_STATE_FIELDS
        )
        record_actor(integration.pk, source, state)

    scalar_models = (
        (
            OrganizationContentConfig,
            "connected_slack_user_id",
            "content_factory.OrganizationContentConfig.connected_slack_user_id",
        ),
        (
            ScheduledDiscoveryDispatch,
            "slack_user_id",
            "content_factory.ScheduledDiscoveryDispatch.slack_user_id",
        ),
    )
    for model, field_name, source_name in scalar_models:
        records = model.objects.only("pk", field_name).iterator(chunk_size=500)
        for record in records:
            actor_id = getattr(record, field_name)
            source = f"{source_name}#{record.pk}"
            record_actor(actor_id, source, actor_id)

    json_models = (
        (
            ContentFactoryJob,
            "slack_user_id",
            "request_meta",
            "content_factory.ContentFactoryJob",
        ),
        (
            ContentFactoryRun,
            "slack_user_id",
            "run_request",
            "workflow_runs.ContentFactoryRun",
        ),
    )
    for model, actor_field, json_field, source_name in json_models:
        records = model.objects.only("pk", actor_field, json_field).iterator(
            chunk_size=500
        )
        for record in records:
            direct_actor_id = getattr(record, actor_field)
            direct_source = f"{source_name}.{actor_field}#{record.pk}"
            record_actor(direct_actor_id, direct_source, direct_actor_id)

            payload = getattr(record, json_field)
            payload_source = f"{source_name}.{json_field}#{record.pk}"
            for actor_id in _internal_actor_ids_in_payload(payload):
                record_actor(actor_id, payload_source, payload)

    if not orphan_sources:
        return

    findings = [
        f"{actor_id}[{','.join(sorted(sources))}]"
        for actor_id, sources in sorted(orphan_sources.items())
    ]
    attestation_fingerprint = _attestation_fingerprint(
        findings,
        state_fingerprints,
    )
    supplied_attestation = (
        str(
            getattr(
                settings,
                "CORE_ACTOR_MIGRATION_HISTORY_ATTESTATION",
                "",
            )
            or ""
        )
        .strip()
        .lower()
    )
    if hmac.compare_digest(supplied_attestation, attestation_fingerprint):
        print(
            "core orphaned actor migration history attestation accepted "
            f"fingerprint={attestation_fingerprint}"
        )
        return

    raise RuntimeError(
        "core.0066 detected internal actor references whose core.User principal "
        "no longer exists. The references may be ownership state left by a "
        "historical core.0063 body and cannot be repaired safely without owner "
        f"evidence. Follow {RECOVERY_RUNBOOK}. "
        "orphaned_internal_actor_history="
        + ";".join(findings)
        + f" attestation_fingerprint={attestation_fingerprint}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0065_recheck_legacy_actor_migration_attestation"),
    ]

    operations = [
        migrations.RunPython(
            guard_orphaned_actor_migration_history,
            migrations.RunPython.noop,
        ),
    ]
