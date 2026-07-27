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
