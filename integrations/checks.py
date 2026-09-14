from django.core.checks import Error, Tags, register

from .fields import _configured_keyring


@register(Tags.security)
def connector_credential_encryption_check(app_configs, **kwargs):
    try:
        _configured_keyring()
    except Exception as exc:
        return [
            Error(
                str(exc),
                id="integrations.E001",
                hint=(
                    "Configure a JSON Fernet keyring in CONNECTOR_CREDENTIAL_KEYS "
                    "and select CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID before deployment."
                ),
            )
        ]
    return []


@register(Tags.security)
def message_sync_configuration_check(app_configs, **kwargs):
    from django.conf import settings
    if not getattr(settings, "MESSAGE_SYNC_ENABLED", False):
        return []
    required = ["MESSAGE_SYNC_SLACK_APP_ID", "MESSAGE_SYNC_SLACK_APP_TOKEN"]
    user_app = getattr(settings, "MESSAGE_SYNC_SLACK_USER_APP_ID", "")
    if user_app and user_app != getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", ""):
        required += ["MESSAGE_SYNC_SLACK_USER_APP_TOKEN", "MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET"]
    if getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", ""):
        required.append("MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID")
    errors = [Error(
        f"{name} is required when durable message sync is enabled.",
        id="integrations.E002", hint="Deploy the approved migration and configure the same Slack app on every worker.",
    ) for name in required if not str(getattr(settings, name, "") or "").strip()]
    if getattr(settings, "MESSAGE_SYNC_SLACK_DISTRIBUTION", "restricted") not in {"restricted", "internal", "marketplace"}:
        errors.append(Error("Invalid MESSAGE_SYNC_SLACK_DISTRIBUTION.", id="integrations.E003"))
    return errors
