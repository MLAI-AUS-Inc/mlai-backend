import re

from django.db import migrations


LEGACY_ACTOR_PATTERN = re.compile(r"web_([1-9][0-9]*)\Z")
CANONICAL_ACTOR_PATTERN = re.compile(r"mlai_user:([1-9][0-9]*)\Z")
ACTOR_JSON_FIELDS = {
    "actor_id",
    "connected_slack_user_id",
    "requested_by_slack_user_id",
    "slack_user_id",
}
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


def _canonical_actor_id(legacy_actor_id, valid_user_ids):
    match = LEGACY_ACTOR_PATTERN.fullmatch(str(legacy_actor_id or ""))
    if not match:
        return None
    user_id = int(match.group(1))
    if user_id not in valid_user_ids:
        return None
    return f"mlai_user:{user_id}"


def _integration_bundle(integration):
    return tuple(
        getattr(integration, field_name)
        for field_name in INTEGRATION_STATE_FIELDS
    )


def _actor_ids_in_payload(value):
    actor_ids = set()
    if isinstance(value, list):
        for item in value:
            actor_ids.update(_actor_ids_in_payload(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ACTOR_JSON_FIELDS and isinstance(item, str):
                actor_ids.add(item)
            elif key not in ACTOR_JSON_FIELDS:
                actor_ids.update(_actor_ids_in_payload(item))
    return actor_ids


def _contains_ambiguous_non_actor_marker(value, valid_user_ids):
    if isinstance(value, str):
        match = CANONICAL_ACTOR_PATTERN.fullmatch(value)
        return bool(match and int(match.group(1)) in valid_user_ids)
    if isinstance(value, list):
        return any(
            _contains_ambiguous_non_actor_marker(item, valid_user_ids)
            for item in value
        )
    if isinstance(value, dict):
        return any(
            _contains_ambiguous_non_actor_marker(item, valid_user_ids)
            for key, item in value.items()
            if key not in ACTOR_JSON_FIELDS
        )
    return False


def guard_legacy_actor_migration_history(apps, schema_editor):
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

    valid_user_ids = set(User.objects.values_list("pk", flat=True).iterator())

    # Earlier committed bodies of core.0063 rewrote references before deciding
    # whether the legacy and canonical integration rows represented distinct
    # GitHub authorities. If those bundles still differ, a canonical reference
    # may now point at the wrong grant and cannot be reassigned without evidence.
    ambiguous_collisions = {}
    legacy_integrations = UserIntegration.objects.filter(
        slack_user_id__startswith="web_"
    ).order_by("slack_user_id")
    for legacy in legacy_integrations.iterator(chunk_size=500):
        canonical_actor_id = _canonical_actor_id(legacy.pk, valid_user_ids)
        if canonical_actor_id is None:
            continue
        canonical = UserIntegration.objects.filter(
            pk=canonical_actor_id
        ).first()
        if canonical is None:
            continue
        if _integration_bundle(legacy) != _integration_bundle(canonical):
            ambiguous_collisions[canonical_actor_id] = legacy.pk

    reference_sources = {}
    for canonical_actor_id, legacy_actor_id in ambiguous_collisions.items():
        reference_sources[canonical_actor_id] = set()
        reference_sources[legacy_actor_id] = set()
    candidate_actor_ids = set(reference_sources)

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
        if not candidate_actor_ids:
            break
        values = model.objects.filter(
            **{f"{field_name}__in": candidate_actor_ids}
        ).values_list(field_name, flat=True)
        for actor_id in set(values):
            reference_sources[actor_id].add(source_name)

    ambiguous_non_actor_sources = set()
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
            if direct_actor_id in candidate_actor_ids:
                reference_sources[direct_actor_id].add(
                    f"{source_name}.{actor_field}"
                )

            payload = getattr(record, json_field)
            for actor_id in _actor_ids_in_payload(payload) & candidate_actor_ids:
                reference_sources[actor_id].add(f"{source_name}.{json_field}")

            # The first committed core.0063 body recursively changed every JSON
            # string, including topics and arbitrary user content. A canonical
            # marker outside a named actor field may therefore be lossy history.
            if _contains_ambiguous_non_actor_marker(payload, valid_user_ids):
                ambiguous_non_actor_sources.add(f"{source_name}.{json_field}")

    collision_findings = []
    unproven_integration_findings = []
    for canonical_actor_id, legacy_actor_id in sorted(
        ambiguous_collisions.items()
    ):
        canonical_sources = reference_sources[canonical_actor_id]
        legacy_sources = reference_sources[legacy_actor_id]
        if canonical_sources:
            collision_findings.append(
                f"{legacy_actor_id}->{canonical_actor_id}"
                f"[{','.join(sorted(canonical_sources))}]"
            )
        elif not legacy_sources:
            # A surviving reference to the legacy key is evidence that the
            # current 0063 body preserved the distinct authority. Without that
            # evidence, an older 0063 may already have mixed missing fields into
            # the canonical integration even if no dependent reference remains.
            unproven_integration_findings.append(
                f"{legacy_actor_id}->{canonical_actor_id}"
            )

    if (
        collision_findings
        or unproven_integration_findings
        or ambiguous_non_actor_sources
    ):
        details = []
        if collision_findings:
            details.append(
                "ambiguous_collision_references="
                + ";".join(collision_findings[:20])
            )
        if unproven_integration_findings:
            details.append(
                "unproven_integration_history="
                + ";".join(unproven_integration_findings[:20])
            )
        if ambiguous_non_actor_sources:
            details.append(
                "ambiguous_non_actor_json="
                + ",".join(sorted(ambiguous_non_actor_sources))
            )
        raise RuntimeError(
            "core.0064 detected state that may have been changed by an earlier "
            "core.0063 body. Do not guess GitHub credential ownership or rewrite "
            "user content automatically. Follow "
            f"{RECOVERY_RUNBOOK}. "
            + " ".join(details)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0063_canonicalize_legacy_content_factory_actor_ids"),
    ]

    operations = [
        migrations.RunPython(
            guard_legacy_actor_migration_history,
            migrations.RunPython.noop,
        ),
    ]
