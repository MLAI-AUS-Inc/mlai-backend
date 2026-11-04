"""Explicit MLAI-admin source selection; private founder accounts stay private."""
from django.db import transaction
from django.core.cache import cache
from django.utils import timezone
from community_chat.admin_access import require_mlai_admin
from integrations.models import ExternalServiceConnection
from integrations.services.luma import LumaAPIError, LumaConfigurationError
from roo.permissions import is_mlai_admin_user

CRM_KEY = "mlai_crm_source_v1"


@transaction.atomic
def select_crm_source(*, user, organization, provider, connection_id, enabled):
    """Opt an owned Luma calendar into CRM ingestion, without changing reports."""
    require_mlai_admin(user)
    if provider != "luma" or not isinstance(enabled, bool) or isinstance(connection_id, bool) or not isinstance(connection_id, int):
        raise ValueError("Choose a connected Luma calendar and whether to include it in CRM.")
    user.__class__.objects.select_for_update().get(pk=user.pk)
    from integrations.services.source_accounts import preserve_report_account
    preserve_report_account(user, organization, provider, ExternalServiceConnection.objects.filter(
        user=user, organization=organization, provider=provider).exclude(status="disconnected"))
    row = ExternalServiceConnection.objects.select_for_update().filter(pk=connection_id,
        user=user, organization=organization, provider=provider).exclude(status="disconnected").first()
    if row is None:
        raise ValueError("That calendar is not connected to this startup.")
    metadata = dict(row.provider_metadata or {})
    metadata[CRM_KEY] = {"enabled": enabled, "selected_by": user.pk, "selected_at": timezone.now().isoformat()}
    row.provider_metadata = metadata
    row.save(update_fields=["provider_metadata", "updated_at"])
    from .models import CrmAuditEvent
    from .luma_sync import SYNC_RESUME_KEY, SYNC_THROTTLE_KEY
    CrmAuditEvent.objects.create(actor=user, action=CrmAuditEvent.Action.SYNC,
        target_type="luma_calendar", detail={"source_set_changed": True, "connection_id": row.pk})
    # A resumed old pass cannot establish freshness for the new source set.
    transaction.on_commit(lambda: cache.delete_many([SYNC_RESUME_KEY, SYNC_THROTTLE_KEY]))
    return row


def selected_luma_sources():
    """Recheck the selecting admin is still active before exposing credentials."""
    rows = ExternalServiceConnection.objects.filter(provider="luma",
        provider_metadata__mlai_crm_source_v1__enabled=True).exclude(status="disconnected").select_related("user")
    return [row for row in rows if row.user.is_active and is_mlai_admin_user(row.user)
        and (row.provider_metadata or {}).get(CRM_KEY, {}).get("selected_by") == row.user_id]


class CombinedLumaSource:
    """Deduplicate events and route guests only to a source with manage access.

    A failed calendar read aborts the catalogue before any rows are replaced.
    Resumed passes use saved provenance, intersected with currently enabled
    admin sources. No fallback to an unrelated or private connection is allowed.
    """
    preserve_preferences = True

    def __init__(self, sources):
        self.sources = dict(sources)
        self.routes = {}

    def list_crm_events(self):
        by_id = {}
        provenance = {}
        for source_id, service in self.sources.items():
            for raw in service.list_crm_events():
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                event_id = str(raw["id"])
                provenance.setdefault(event_id, {})[source_id] = {
                    "access": raw.get("access"), "platform": raw.get("platform")}
                current = by_id.get(event_id)
                manageable = raw.get("access") == "manage" and raw.get("platform") == "luma"
                if current is None or manageable and current.get("access") != "manage":
                    by_id[event_id] = dict(raw)
        for event_id, raw in by_id.items():
            raw["crm_sources"] = provenance[event_id]
            self.routes[event_id] = self._managed_sources(provenance[event_id])
        return list(by_id.values())

    def _managed_sources(self, provenance):
        return [key for key, value in provenance.items() if key in self.sources
            and isinstance(value, dict) and value.get("access") == "manage" and value.get("platform") == "luma"]

    def can_read_guests(self, raw):
        """Recheck current selected accounts before a queued guest read."""
        provenance = raw.get("crm_sources")
        if not isinstance(provenance, dict):
            provenance = {"community": {"access": raw.get("access"), "platform": raw.get("platform")}}
        return bool(self._managed_sources(provenance))

    def list_guests(self, *, event_id, approval_status=None):
        if event_id not in self.routes:
            from crm.models import CrmEvent
            raw = CrmEvent.objects.filter(luma_event_id=event_id).values_list("raw_payload", flat=True).first() or {}
            provenance = raw.get("crm_sources")
            if not isinstance(provenance, dict):
                # Existing rows predate per-source provenance and belong only
                # to the configured community calendar, never a founder key.
                provenance = {"community": {"access": raw.get("access"), "platform": raw.get("platform")}}
            self.routes[event_id] = self._managed_sources(provenance)
        candidates = self.routes[event_id]
        if not candidates:
            raise LumaAPIError("No selected source can read this event's guests.", status_code=403)
        # One authoritative complete guest list; duplicate calendar listings
        # never multiply registrations or activity evidence.
        return self.sources[candidates[0]].list_guests(event_id=event_id, approval_status=approval_status)


def build_combined_luma_source(community_service, make_service):
    sources = {}
    if community_service is not None:
        sources["community"] = community_service
    for row in selected_luma_sources():
        if not row.access_token:
            raise LumaConfigurationError("A selected CRM calendar needs to be reconnected.")
        sources[f"connection:{row.pk}"] = make_service(row.access_token)
    if not sources:
        raise LumaConfigurationError("No CRM calendars are configured.")
    return CombinedLumaSource(sources)
