import re

from django.db import migrations


LEGACY_ACTOR_PATTERN = re.compile(r"web_([1-9][0-9]*)\Z")
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
CREDENTIAL_AUTHORITY_FIELDS = (
    "github_access_token",
    "github_refresh_token",
    "github_installation_id",
)


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
    if isinstance(value, list):
        return [_replace_actor_ids(item, valid_user_ids) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                _canonical_actor_id(item, valid_user_ids)
                if key in ACTOR_JSON_FIELDS
                else _replace_actor_ids(item, valid_user_ids)
            )
            for key, item in value.items()
        }
    return value


def _is_blank(value):
    return value is None or value is False or value == "" or value == [] or value == {}


def _copy_legacy_integration_bundle_when_safe(
    UserIntegration,
    legacy,
    canonical,
):
    # Tokens, refresh tokens, expiry, scopes, installation and repository form
    # one authorization bundle. Never fill them field-by-field across two rows:
    # they may represent different GitHub grants or accounts. Only copy the
    # complete legacy state when the canonical row has no credential authority
    # of its own. Non-authoritative canonical metadata is replaced as part of
    # that one bundle instead of being combined with the legacy grant.
    canonical_has_credentials = any(
        not _is_blank(getattr(canonical, field_name))
        for field_name in CREDENTIAL_AUTHORITY_FIELDS
    )
    legacy_has_credentials = any(
        not _is_blank(getattr(legacy, field_name))
        for field_name in CREDENTIAL_AUTHORITY_FIELDS
    )
    canonical_is_blank = all(
        _is_blank(getattr(canonical, field_name))
        for field_name in INTEGRATION_STATE_FIELDS
    )
    if canonical_has_credentials or not (
        legacy_has_credentials or canonical_is_blank
    ):
        return
    updates = {
        field_name: getattr(legacy, field_name)
        for field_name in INTEGRATION_STATE_FIELDS
    }
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
        # Both rows may carry different live credentials. Keep each non-empty
        # bundle intact. An uncredentialed canonical row may adopt the complete
        # legacy bundle, replacing its metadata rather than mixing grants.
        _copy_legacy_integration_bundle_when_safe(
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
