"""Current Members-source exclusion, including previously retained evidence.

Keep ordinary managers unfiltered so revocation, reconciliation and deletion can
still inspect every retained row. Consumers explicitly use these SQL fences.
"""
from django.db.models import CharField, Exists, F, OuterRef, Q, Value
from django.db.models.functions import Cast, Concat, Replace
from django.db.models.fields.json import KeyTextTransform

from integrations.models import CommunityBridgeChannel
from integrations.services.slack_source_privacy import is_members_source
from .models import MemoryClaim, MemoryPublication, MemorySource, MemorySummary


def members_source_ids():
    # Stable source identity survives removal of its configuration/source_scope.
    mappings = CommunityBridgeChannel.objects.filter(
        audience="members", slack_workspace_id=OuterRef("external_account_id"),
    ).annotate(_source_external_id=Cast(OuterRef("external_id"), CharField())).filter(
        Q(slack_channel_id=OuterRef("source_scope__external_id"))
        | Q(slack_channel_id=OuterRef("_members_source_channel"))
        | Q(_source_external_id__startswith=Concat(Value("slack_thread:"), F("slack_channel_id"), Value(":")))
    )
    return MemorySource.objects.filter(provider="slack").annotate(
        _members_source_channel=KeyTextTransform("channel_id", "metadata"),
    ).alias(
        _members_bridge_source=Exists(mappings),
    ).filter(_members_bridge_source=True).values("pk")


def exclude_members_evidence(queryset, *, source_path="source"):
    return queryset.exclude(**{source_path + "__in": members_source_ids()})


def exclude_members_claims(queryset):
    # A mixed-lineage statement may contain Members text even if another piece
    # of evidence is Community. Do not authorize the statement by that sibling.
    return exclude_members_evidence(queryset, source_path="evidence__source")


def claims_have_members(queryset):
    return queryset.filter(evidence__source__in=members_source_ids()).exists()


def summary_has_members(summary):
    return (claims_have_members(MemoryClaim.objects.filter(summary_links__summary=summary))
            or summary.evidence_links.filter(evidence__source__in=members_source_ids()).exists())


def digest_has_members(digest):
    return (claims_have_members(MemoryClaim.objects.filter(digest_items__digest=digest))
            or digest.items.filter(evidence_links__evidence__source__in=members_source_ids()).exists())


def members_publications(queryset=None):
    claims = MemoryClaim.objects.filter(evidence__source__in=members_source_ids()).distinct()
    summaries = MemorySummary.objects.filter(
        Q(claim_links__claim__in=claims) | Q(evidence_links__evidence__source__in=members_source_ids()),
    ).distinct()
    def normalized_ids(rows):
        # Generic object IDs are strings; SQLite's UUID storage omits hyphens.
        return rows.annotate(_members_id=Replace(Cast("pk", CharField()), Value("-"), Value(""))).values("_members_id")
    rows = MemoryPublication.objects.all() if queryset is None else queryset
    return rows.alias(_members_object_id=Replace("source_object_id", Value("-"), Value(""))).filter(
        Q(source_content_type__app_label="org_memory", source_content_type__model="memoryclaim",
          _members_object_id__in=normalized_ids(claims))
        | Q(source_content_type__app_label="org_memory", source_content_type__model="memorysummary",
            _members_object_id__in=normalized_ids(summaries)),
    )


def exclude_members_publications(queryset):
    return queryset.exclude(pk__in=members_publications().values("pk"))


def exclude_members_public_items(queryset):
    return queryset.exclude(pk__in=members_publications().exclude(
        published_item_id__isnull=True,
    ).values("published_item_id"))


def action_has_members(proposal):
    from uuid import UUID
    try:
        claims = [UUID(str(value)) for value in proposal.evidence_claim_ids or []]
        sources = [UUID(str(value)) for value in proposal.evidence_source_ids or []]
    except (ValueError, TypeError, AttributeError):
        return True
    return (members_source_ids().filter(pk__in=sources).exists()
            or claims_have_members(MemoryClaim.objects.filter(pk__in=claims)))


def source_is_members(source):
    if source.provider != "slack":
        return False
    return identity_is_members(
        external_account_id=source.external_account_id, external_id=source.external_id,
        metadata=source.metadata, source_scope=source.source_scope if source.source_scope_id else None,
    )


def identity_is_members(*, external_account_id, external_id, metadata=None, source_scope=None):
    candidates = [(metadata or {}).get("channel_id")] if isinstance(metadata, dict) else []
    if source_scope is not None:
        candidates.append(source_scope.external_id)
    if isinstance(external_id, str) and external_id.startswith("slack_thread:"):
        candidates.append(external_id.split(":", 2)[1])
    return any(is_members_source(external_account_id, value) for value in candidates
               if isinstance(value, str) and value)
