"""Check every explicitly selected provider before admitting expensive generation."""

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from datetime import timedelta

from integrations import http_client as requests
from django.db import close_old_connections, connections
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

PROVIDERS = frozenset(
    {
        "gmail",
        "slack",
        "linear",
        "notion",
        "google_analytics",
        "xero",
        "stripe",
        "bank_feed",
        "humanitix",
        "luma",
        "manual_documents",
    }
)


class SourcesNotReady(APIException):
    """Aggregate safe, actionable results without exposing upstream responses."""

    status_code = 409
    default_code = "startup_update_sources_unavailable"


class ManualInputNotReady(ValueError):
    """Selected founder documents are missing or have not finished parsing."""


def rate_limited_check(provider, retry_after_seconds):
    """Expose a safe provider deadline without treating quota as revoked OAuth."""
    seconds = max(int(retry_after_seconds), 1)
    retry_at = timezone.now() + timedelta(seconds=seconds)
    message = (
        f"{provider.replace('_', ' ').title()} API rate limit reached. "
        f"Pulse will check again after {retry_at:%d %b %Y, %H:%M:%S UTC}."
    )
    if provider == "xero":
        message += " Reconnecting Xero does not reset this limit."
    return {"provider": provider, "status": "temporarily_unavailable",
        "reason": "rate_limited", "message": message,
        "retry_after_seconds": seconds, "retry_at": retry_at.isoformat()}


def apply_xero_cooldown(checks):
    """Retain a quota hit during preparation/report refresh at the same barrier."""
    return [
        {**check, **rate_limited_check("xero", seconds)}
        if check.get("provider") == "xero" and (seconds := requests.xero_cooldown_seconds(check.get("accountId")))
        else check
        for check in checks
    ]


def selected_sources(values, *, manual=False):
    """Normalize explicit input without ever substituting another provider."""
    if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
        raise ValidationError({"inputSources": "Choose supported update sources."})
    result = list(dict.fromkeys(x.strip().lower().replace("-", "_") for x in values))
    if set(result) - (PROVIDERS | {"google_drive"}):
        raise ValidationError({"inputSources": "Choose supported update sources."})
    if manual and "manual_documents" not in result:
        result.append("manual_documents")
    if not result:
        raise ValidationError(
            {"inputSources": "Select a connection or add notes or documents."}
        )
    return result


SOURCE_LABELS = {
    "gmail": "Gmail", "slack": "Slack", "linear": "Linear", "notion": "Notion",
    "google_analytics": "Google Analytics", "xero": "Xero", "stripe": "Stripe",
    "bank_feed": "your bank feed", "humanitix": "Humanitix", "luma": "Luma",
    "manual_documents": "your documents", "google_drive": "Google Drive",
}
# Founder notes and documents are the founder's own input. A draft never
# silently leaves them out, so they are never skipped.
UNSKIPPABLE_SOURCES = frozenset({"manual_documents"})


def source_label(provider):
    return SOURCE_LABELS.get(provider, str(provider or "").replace("_", " ").title())


def skipped_source_entry(check, *, stage="check"):
    """A founder-safe record of a tool MLAI left out of this draft."""
    provider = check.get("provider")
    label = source_label(provider)
    status = check.get("status")
    if stage == "collection":
        code, action = "collection_failed", "retry"
        message = f"MLAI could not finish reading {label}, so this draft may miss some of it."
    elif status == "needs_reconnect":
        code, action = "needs_reconnect", "reconnect"
        message = f"{label[:1].upper()}{label[1:]} needs reconnecting, so MLAI drafted this update without it."
    elif status == "unsupported":
        code, action = "unsupported", None
        message = f"MLAI cannot read {label} yet, so this draft leaves it out."
    else:
        code, action = (check.get("reason") or "temporarily_unavailable"), "retry"
        message = f"MLAI could not read {label} just now, so this draft leaves it out."
    return {"provider": provider, "code": code, "message": message, "action": action}


def run_skipped_sources(run_request, result=None):
    """Tools left out of a run: by the backend check, or by the worker during reading."""
    skipped = {}
    for group in ((run_request or {}).get("skipped_sources"), (result or {}).get("skipped_sources")):
        if isinstance(group, dict):
            for key, value in group.items():
                if isinstance(value, dict):
                    skipped.setdefault(key, value)
    return skipped


def effective_input_sources(run_request, result=None):
    """The selected tools this run still reads, in selection order."""
    skipped = run_skipped_sources(run_request, result)
    return [source for source in (run_request or {}).get("input_sources") or [] if source not in skipped]


def skip_failed_checks(checks, skipped):
    """Skip every failing tool that may be skipped; report whether any input remains.

    ``skipped`` is updated in place. Returns False when nothing usable is left,
    in which case the caller keeps the run's barrier closed.
    """
    for check in checks:
        provider = check.get("provider")
        if check.get("status") != "ready" and provider not in skipped and provider not in UNSKIPPABLE_SOURCES:
            skipped[provider] = skipped_source_entry(check)
    remaining = [check for check in checks if check.get("provider") not in skipped]
    return bool(remaining) and all(check.get("status") == "ready" for check in remaining)


AUTOMATIC_SOURCE_PROVIDERS = (
    "gmail", "slack", "linear", "notion", "google_analytics",
    "xero", "stripe", "bank_feed", "humanitix", "luma",
)


def connected_update_sources(user, organization):
    """Return every healthy connection Pulse can read for a scheduled draft.

    Used when nobody picked sources by hand (the monthly schedule). A source
    the founder switched off for Pulse stays off, and broken connections are
    left out rather than failing the run.
    """
    from integrations.models import ExternalServiceConnection, ExternalServiceConnectionStatus
    from integrations.services.external_connectors import google_connection_for_org, has_gmail_read_scope

    if organization is None:
        return []
    switched_off = set()
    profile = getattr(organization, "startup_profile", None)
    if profile is not None:
        from community_chat.startups.source_preferences import PREFERENCE_KEY
        choices = ((profile.progress_configuration or {}).get(PREFERENCE_KEY) or {})
        for key, values in choices.items():
            if str(key).endswith(f":{user.pk}") and isinstance(values, dict):
                switched_off.update(provider for provider, enabled in values.items() if enabled is False)
    found = set()
    google = google_connection_for_org(user, organization)
    if google is not None and has_gmail_read_scope(google):
        found.add("gmail")
    found.update(ExternalServiceConnection.objects.filter(
        user=user, organization=organization, provider__in=AUTOMATIC_SOURCE_PROVIDERS,
        status__in=[ExternalServiceConnectionStatus.CONNECTED, ExternalServiceConnectionStatus.SYNCING],
    ).values_list("provider", flat=True))
    return [provider for provider in AUTOMATIC_SOURCE_PROVIDERS if provider in found and provider not in switched_off]


def _get(url, *, token, headers=None, params=None):
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
        params=params,
        timeout=(3, 15),
    )
    response.raise_for_status()
    return response.json()


def probe_source(provider, *, user, organization):
    """Make a bounded relevant read using only this user's startup connection."""
    from integrations.models import ExternalServiceConnection
    from integrations.services import external_connectors as ec

    if provider == "gmail":
        from integrations.services.external_connectors import google_connection_for_org
        from integrations.services.gmail import list_message_page

        connection = google_connection_for_org(user, organization)
        from integrations.services.external_connectors import has_gmail_read_scope

        if connection is None or not has_gmail_read_scope(connection):
            raise PermissionError()
        list_message_page(connection, query="", max_results=1)
        return {"connectionId": connection.pk}
    from integrations.services.source_accounts import report_connection
    connection = report_connection(ExternalServiceConnection.objects.filter(
        user=user, organization=organization, provider=provider,
    ).exclude(status="disconnected"), user=user, organization=organization, provider=provider)
    if connection is None:
        raise PermissionError()
    if provider == "slack":
        catalog = ec._slack_api_request(
            connection,
            "conversations.list",
            params={"limit": 1, "exclude_archived": "true"},
        )
        channels = catalog.get("channels") or []
        if channels:
            ec._slack_api_request(
                connection,
                "conversations.history",
                params={"channel": channels[0]["id"], "limit": 1},
            )
    elif provider == "linear":
        ec._linear_graphql_request(
            connection, "query { projects(first: 1) { nodes { id } } }"
        )
    elif provider == "notion":
        response = requests.post(
            "https://api.notion.com/v1/search",
            headers={
                "Authorization": f"Bearer {connection.access_token}",
                "Notion-Version": "2026-03-11",
            },
            json={"page_size": 1},
            timeout=(3, 15),
        )
        response.raise_for_status()
        pages = response.json().get("results") or []
        if pages:
            _get(
                f"https://api.notion.com/v1/blocks/{pages[0]['id']}/children",
                token=connection.access_token,
                headers={"Notion-Version": "2026-03-11"},
                params={"page_size": 1},
            )
    elif provider == "google_analytics":
        ec.serialize_google_analytics_properties(
            user, organization=organization, limit=1
        )
    elif provider == "xero":
        _get(
            "https://api.xero.com/api.xro/2.0/Reports/ProfitAndLoss",
            token=ec._xero_required_token(connection),
            headers={
                "xero-tenant-id": connection.external_account_id,
                "Accept": "application/json",
            },
        )
    elif provider == "stripe":
        _get(
            "https://api.stripe.com/v1/invoices",
            token=connection.access_token,
            params={"limit": 1},
        )
        _get(
            "https://api.stripe.com/v1/subscriptions",
            token=connection.access_token,
            params={"limit": 1},
        )
    elif provider == "bank_feed":
        from urllib.parse import quote

        token = ec._basiq_token(scope="SERVER_ACCESS").get("access_token")
        if not token or not connection.external_account_id:
            raise PermissionError()
        user_id = quote(connection.external_account_id, safe="")
        ec._basiq_get_json(f"/users/{user_id}/accounts", token, {"limit": 1})
        ec._basiq_get_json(
            f"/users/{user_id}/transactions",
            token,
            {"limit": 1, "filter": "status.eq('posted')"},
        )
    elif provider == "humanitix":
        from integrations.services.humanitix import HumanitixClient

        HumanitixClient(
            api_key=connection.access_token, timeout=(3, 15)
        ).list_events_page(page_size=1)
    elif provider == "luma":
        from integrations.services.luma import LumaAttendeeReportService

        LumaAttendeeReportService(api_key=connection.access_token, timeout=15)._get(
            "/v1/calendar/list-events", params={"pagination_limit": 1}
        )
    else:
        raise NotImplementedError()
    return {"connectionId": connection.pk, "accountId": connection.external_account_id}


def source_failure_check(provider, exc):
    """Use the same safe failure contract for probes, discovery and preparation."""
    from integrations.services import external_connectors as ec

    response = getattr(exc, "response", None)
    if response is None:
        response = getattr(exc, "resp", None)
    status = getattr(response, "status_code", None) or getattr(response, "status", None) or getattr(exc, "status_code", None)
    if status == 429 or isinstance(exc, ec.ConnectorRateLimitError):
        seconds = getattr(exc, "retry_after_seconds", None)
        if seconds is None:
            headers = getattr(response, "headers", response if isinstance(response, Mapping) else {})
            seconds = requests._retry_after_seconds(headers.get("Retry-After") or headers.get("retry-after"))
        return rate_limited_check(provider, seconds)
    if isinstance(exc, NotImplementedError):
        code, message = "unsupported", "This connection does not support Pulse imports yet."
    elif isinstance(exc, ManualInputNotReady):
        code, message = "needs_input", "Add notes or wait for selected documents to finish processing."
    elif status in (401, 403) or (isinstance(exc, (PermissionError, ec.ConnectorOAuthError)) and not isinstance(exc, ec.ConnectorTemporarilyUnavailable)):
        code, message = "needs_reconnect", "Reconnect this source and check its required permissions."
    else:
        code, message = "temporarily_unavailable", "This source could not be checked. Try checking it again shortly."
    return {"provider": provider, "status": code, "message": message}


def check_sources(sources, probe):
    """Return every outcome in selection order, even when several probes fail."""

    def check(provider):
        try:
            details = probe(provider) or {}
            return {"provider": provider, "status": "ready", **details}
        except Exception as exc:  # noqa: BLE001 - aggregate every independent provider failure safely
            return source_failure_check(provider, exc)

    # No shared result mutations; executor.map preserves selection order.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(sources)))) as pool:
        return list(pool.map(check, sources))


def require_source_preflight(
    *, user, organization, input_sources, manual_document_ids=None, manual_summary="", connection_pins=None
):
    """Check every selected source before full collection or generation."""
    sources = selected_sources(
        input_sources,
        manual=bool(manual_document_ids or str(manual_summary or "").strip()),
    )

    def probe(provider):
        close_old_connections()
        try:
            if provider == "google_drive":
                raise NotImplementedError()
            if provider == "manual_documents":
                from django.db.models import Q

                from startup_updates.models import StartupManualDocument

                ids = set(map(str, manual_document_ids or []))
                documents = list(
                    StartupManualDocument.objects.filter(
                        pk__in=ids, organization=organization
                    ).filter(Q(created_by=user) | Q(company__profile__user=user))
                )
                if len(documents) != len(ids) or any(
                    d.extraction_status not in {"processed", "hydrated"}
                    or not d.extracted_text.strip()
                    for d in documents
                ):
                    raise ManualInputNotReady()
                if not documents and not str(manual_summary or "").strip():
                    raise ManualInputNotReady()
                return {}
            from integrations.services.source_accounts import report_account_scope
            with report_account_scope(user, organization, connection_pins):
                return probe_source(provider, user=user, organization=organization)
        finally:
            # These short-lived executor threads are outside Django's request
            # teardown. Close even a healthy persistent connection explicitly.
            connections.close_all()

    results = check_sources(sources, probe)
    unavailable = [r["provider"] for r in results if r["status"] != "ready"]
    if unavailable:
        raise SourcesNotReady(
            {
                "code": "startup_update_sources_unavailable",
                "detail": "Check the selected sources below before generating your update.",
                "unavailableSources": unavailable,
                "sourceChecks": results,
            }
        )
    return results
