import re

from django.db import migrations


LEGACY_ACTOR_PATTERN = re.compile(r"web_([1-9][0-9]*)\Z")


def _canonical_actor_id(value, valid_user_ids):
    if not isinstance(value, str):
        return value
    match = LEGACY_ACTOR_PATTERN.fullmatch(value)
    if not match:
        return value
    user_id = int(match.group(1))
    if user_id not in valid_user_ids:
        return value
    return f"mlai_user:{user_id}"


def _replace_actor_ids(value, valid_user_ids):
    if isinstance(value, str):
        return _canonical_actor_id(value, valid_user_ids)
    if isinstance(value, list):
        return [_replace_actor_ids(item, valid_user_ids) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_actor_ids(item, valid_user_ids)
            for key, item in value.items()
        }
    return value


def _is_blank(value):
    return value is None or value == "" or value == [] or value == {}


def _copy_missing_user_integration_fields(UserIntegration, legacy, canonical):
    updates = {}
    fallback_fields = (
        "github_access_token",
        "github_refresh_token",
        "github_token_expires_at",
        "github_user_name",
        "github_repo",
        "github_scopes",
        "github_installation_id",
        "last_scanned_sha",
        "pending_intent",
    )
    for field_name in fallback_fields:
        current_value = getattr(canonical, field_name)
        legacy_value = getattr(legacy, field_name)
        if _is_blank(current_value) and not _is_blank(legacy_value):
            updates[field_name] = legacy_value

    if legacy.project_scanned and not canonical.project_scanned:
        updates["project_scanned"] = True
    if legacy.last_scanned_at and (
        not canonical.last_scanned_at
        or legacy.last_scanned_at > canonical.last_scanned_at
    ):
        updates["last_scanned_at"] = legacy.last_scanned_at
        if legacy.last_scanned_sha:
            updates["last_scanned_sha"] = legacy.last_scanned_sha

    if updates:
        UserIntegration.objects.filter(pk=canonical.pk).update(**updates)


def canonicalize_legacy_actor_ids(apps, schema_editor):
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

    scalar_models = (
        (OrganizationContentConfig, "connected_slack_user_id"),
        (ScheduledDiscoveryDispatch, "slack_user_id"),
    )
    for model, field_name in scalar_models:
        for record in model.objects.only("pk", field_name).iterator(chunk_size=500):
            current_value = getattr(record, field_name)
            canonical_value = _canonical_actor_id(current_value, valid_user_ids)
            if canonical_value != current_value:
                model.objects.filter(pk=record.pk).update(
                    **{field_name: canonical_value}
                )

    json_models = (
        (ContentFactoryJob, "slack_user_id", "request_meta"),
        (ContentFactoryRun, "slack_user_id", "run_request"),
    )
    for model, actor_field, json_field in json_models:
        for record in model.objects.only(
            "pk", actor_field, json_field
        ).iterator(chunk_size=500):
            updates = {}
            current_actor = getattr(record, actor_field)
            canonical_actor = _canonical_actor_id(current_actor, valid_user_ids)
            if canonical_actor != current_actor:
                updates[actor_field] = canonical_actor

            current_payload = getattr(record, json_field)
            canonical_payload = _replace_actor_ids(
                current_payload,
                valid_user_ids,
            )
            if canonical_payload != current_payload:
                updates[json_field] = canonical_payload

            if updates:
                model.objects.filter(pk=record.pk).update(**updates)

    integrations = list(
        UserIntegration.objects.filter(slack_user_id__startswith="web_").iterator(
            chunk_size=500
        )
    )
    for legacy in integrations:
        canonical_id = _canonical_actor_id(legacy.pk, valid_user_ids)
        if canonical_id == legacy.pk:
            continue
        canonical = UserIntegration.objects.filter(pk=canonical_id).first()
        if canonical is None:
            UserIntegration.objects.filter(pk=legacy.pk).update(
                slack_user_id=canonical_id
            )
            continue
        # Both rows may carry different live credentials. Fill gaps in the
        # canonical row, but retain the legacy alias rather than guessing which
        # non-empty credential set is authoritative.
        _copy_missing_user_integration_fields(
            UserIntegration,
            legacy,
            canonical,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0062_slackfounderlinkrequest_created_index"),
        ("content_factory", "0037_content_islands"),
        ("integrations", "0041_linear_project_sizing_runs"),
        ("workflow_runs", "0005_contentfactoryrun_reconciled_at"),
    ]

    operations = [
        migrations.RunPython(
            canonicalize_legacy_actor_ids,
            migrations.RunPython.noop,
        ),
    ]
