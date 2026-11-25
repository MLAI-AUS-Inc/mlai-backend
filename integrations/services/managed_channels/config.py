"""Only named, explicitly enabled installations can admit provider work."""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def installations():
    configured = getattr(settings, "MANAGED_CHANNEL_SOURCES", {})
    if not isinstance(configured, dict):
        raise ImproperlyConfigured("Invalid managed channel sources.")
    result, identities = {}, set()
    for name, item in configured.items():
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        identity = tuple(item.get(field) for field in ("community", "provider", "scope"))
        if (any(not isinstance(value, str) or not value.strip() or len(value) > 255 for value in identity)
                or identity[1] not in {"linear", "plane", "luma"} or identity in identities):
            raise ImproperlyConfigured("Invalid or duplicate managed channel installation.")
        identities.add(identity)
        result[name] = item
    return result


def scope_config(scope):
    identity = (scope.community, scope.provider, scope.scope)
    return next((c for c in installations().values()
                 if (c["community"], c["provider"], c["scope"]) == identity), None)
