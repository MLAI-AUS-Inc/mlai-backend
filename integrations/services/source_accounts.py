"""Account identity and explicit report choices within one user's startup."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from integrations.models import ExternalServiceConnection, GoogleConnection
from startup_updates.models import StartupProfile

REPORT_ACCOUNTS_KEY = "report_source_accounts"
_RUN_ACCOUNTS = ContextVar("mlai_report_run_accounts", default=None)


@contextmanager
def report_account_scope(user, organization, pins):
    """Bind owned selectors to immutable run pins for one execution only."""
    if pins is None:
        yield
        return
    token = _RUN_ACCOUNTS.set((user.pk, getattr(organization, "pk", None), dict(pins)))
    try:
        yield
    finally:
        _RUN_ACCOUNTS.reset(token)



def report_account_id(user, organization, provider):
    """Return an explicit choice, or None for an existing legacy default."""
    scope = _RUN_ACCOUNTS.get()
    if scope is not None and scope[:2] == (user.pk, getattr(organization, "pk", None)):
        return scope[2].get(provider) or 0
    if organization is None:
        return None
    profile = StartupProfile.objects.filter(organization=organization).first()
    choices = (profile.progress_configuration or {}).get(REPORT_ACCOUNTS_KEY, {}) if profile else {}
    value = choices.get(str(user.pk), {}).get(provider)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def metric_account_filter(organization, provider, connection_id):
    """Include unscoped legacy metrics only for an unambiguous sole account.

    Disconnected identities still count: their old metrics must never become
    attributed to a newly connected sibling.
    """
    scope = Q(source_metadata__connection_id=connection_id)
    ids = list(ExternalServiceConnection.objects.filter(organization=organization,
        provider=provider).values_list("pk", flat=True)[:2])
    if ids == [connection_id] or not ids and connection_id is None:
        scope |= Q(source_metadata__connection_id__isnull=True) | Q(source_metadata__connection_id=None)
    return scope


def report_connection(queryset, *, user, organization, provider):
    """Resolve a report's account without silently replacing a missing choice."""
    selected = report_account_id(user, organization, provider)
    if selected is not None:
        return queryset.filter(pk=selected).first()
    return queryset.order_by("-updated_at", "-pk").first()


def _save_report_account(user, organization, provider, connection_id):
    profile, _ = StartupProfile.objects.select_for_update().get_or_create(organization=organization)
    config = dict(profile.progress_configuration or {})
    people = dict(config.get(REPORT_ACCOUNTS_KEY) or {})
    choices = dict(people.get(str(user.pk)) or {})
    choices[provider] = connection_id
    people[str(user.pk)] = choices
    config[REPORT_ACCOUNTS_KEY] = people
    profile.progress_configuration = config
    profile.save(update_fields=["progress_configuration", "updated_at"])


def preserve_report_account(user, organization, provider, queryset):
    """Pin today's default before adding an account; caller holds the user lock."""
    if organization is None or report_account_id(user, organization, provider) is not None:
        return
    current = queryset.order_by("-updated_at", "-pk").first()
    if current is not None:
        _save_report_account(user, organization, provider, current.pk)


@transaction.atomic
def select_report_account(user, organization, provider, connection_id):
    """Choose an owned account for new reports, preserving existing run pins."""
    if organization is None or not isinstance(connection_id, int) or isinstance(connection_id, bool):
        raise ValueError("Choose an account for this startup.")
    user.__class__.objects.select_for_update().get(pk=user.pk)
    if provider == "gmail":
        queryset = GoogleConnection.objects.filter(user=user, organization=organization)
    else:
        queryset = ExternalServiceConnection.objects.filter(
            user=user, organization=organization, provider=provider,
        ).exclude(status="disconnected")
    connection = queryset.filter(pk=connection_id).first()
    if connection is None:
        raise ValueError("That account is not connected to this startup.")
    _save_report_account(user, organization, provider, connection.pk)
    return connection


@transaction.atomic
def store_google_account(*, user, organization, userinfo, token_data):
    """Store only the Google-authenticated identity's token, never a sibling's."""
    subject = str(userinfo.get("sub") or "").strip()
    email = str(userinfo.get("email") or "").strip().lower()
    if not subject or len(subject) > 255 or userinfo.get("email_verified") is not True:
        raise ValueError("Google did not return a verified account identity. Please reconnect.")
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ValueError("Google did not return a valid account email.") from exc
    user.__class__.objects.select_for_update().get(pk=user.pk)
    queryset = GoogleConnection.objects.filter(user=user, organization=organization)
    preserve_report_account(user, organization, "gmail", queryset)
    connection = queryset.filter(provider_account_id=subject).first()
    if connection is None:
        # The old schema allowed a single unverified row per startup. Adopt it
        # only for that exact verified email, retaining its ID and references.
        legacy = list(queryset.filter(provider_account_id__isnull=True, google_email__iexact=email)[:2])
        connection = legacy[0] if len(legacy) == 1 else None
    refresh_token = token_data.get("refresh_token") or (connection.refresh_token if connection else "")
    if not refresh_token:
        raise ValueError("Google did not return a refresh token for this account. Reconnect with consent.")
    if connection is None:
        connection = GoogleConnection(user=user, organization=organization)
    connection.provider_account_id = subject
    connection.google_email = email
    connection.refresh_token = refresh_token
    connection.scope = token_data.get("scope") or connection.scope or ""
    connection.save()
    return connection


def google_connection_for_binding(binding):
    """Use an explicit report choice, otherwise preserve the legacy binding."""
    from .external_connectors import google_connection_for_org
    if report_account_id(binding.user, binding.organization, "gmail") is not None:
        return google_connection_for_org(binding.user, binding.organization)
    return binding.google_connection or google_connection_for_org(binding.user, binding.organization)


def serialize_accounts(user, organization, provider, queryset, selected):
    """Expose account labels and health, never tokens or provider metadata."""
    selected_id = report_account_id(user, organization, provider)
    if selected_id is None and selected is not None:
        selected_id = selected.pk
    accounts = []
    for row in queryset.order_by("pk"):
        gmail = provider == "gmail"
        accounts.append({
            "connectionId": row.pk,
            "accountLabel": row.google_email if gmail else (row.account_label or row.external_account_id),
            "status": "connected" if gmail else row.status,
            "selectedForReports": row.pk == selected_id,
            "crmEnabled": False if gmail else (row.provider_metadata or {}).get("mlai_crm_source_v1", {}).get("enabled") is True,
            "lastSyncedAt": None if gmail or not row.last_synced_at else row.last_synced_at.isoformat(),
        })
    return {"accounts": accounts, "reportAccountId": selected_id,
            "reportAccountMissing": selected_id is not None and not any(row["connectionId"] == selected_id for row in accounts)}


@transaction.atomic
def adopt_api_key_identity(user, organization, provider, api_key, identity):
    """Retain a legacy row only when its existing credential is an exact match."""
    import secrets
    user.__class__.objects.select_for_update().get(pk=user.pk)
    rows = ExternalServiceConnection.objects.filter(user=user, organization=organization, provider=provider)
    if rows.filter(external_account_id=identity).exists():
        return
    matches = [row for row in rows if row.access_token and secrets.compare_digest(row.access_token, api_key)]
    if len(matches) == 1:
        matches[0].external_account_id = identity
        matches[0].save(update_fields=["external_account_id"])


def api_key_connection_identity(user, organization, provider, api_key):
    """Providers without an identity endpoint retain exact credentials separately."""
    import secrets
    import uuid
    for row in ExternalServiceConnection.objects.filter(user=user, organization=organization, provider=provider).order_by("pk"):
        if row.access_token and secrets.compare_digest(row.access_token, api_key):
            return row.external_account_id
    return "credential:" + str(uuid.uuid4())


@transaction.atomic
def adopt_google_external_identity(user, organization, provider, userinfo):
    """Upgrade a legacy email key only with a matching verified Google identity."""
    subject = str(userinfo.get("sub") or "").strip()
    email = str(userinfo.get("email") or "").strip()
    if not subject or len(subject) > 255 or userinfo.get("email_verified") is not True:
        from .external_connectors import ConnectorConfigurationError
        raise ConnectorConfigurationError("Google did not return a verified account identity.")
    user.__class__.objects.select_for_update().get(pk=user.pk)
    rows = ExternalServiceConnection.objects.filter(user=user, organization=organization, provider=provider)
    if rows.filter(external_account_id=subject).exists():
        return
    matches = [row for row in rows if
        (row.provider_metadata or {}).get("userinfo", {}).get("sub") == subject
        or (row.external_account_id.lower() == email.lower() and email)]
    if len(matches) == 1:
        matches[0].external_account_id = subject
        matches[0].save(update_fields=["external_account_id"])


def connection_for_run(run, user, organization, provider):
    """Resolve immutable run pins; changing defaults never selects a sibling."""
    queryset = ExternalServiceConnection.objects.filter(user=user, organization=organization,
        provider=provider).exclude(status="disconnected")
    request = run.run_request or {}
    pins = request.get("source_connection_pins")
    if pins is not None:
        return queryset.filter(pk=pins.get(provider) or 0).first()
    legacy = request.get(f"{provider}_connection_id")
    if legacy is not None:
        return queryset.filter(pk=legacy).first()
    return report_connection(queryset, user=user, organization=organization, provider=provider)
