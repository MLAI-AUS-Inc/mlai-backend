"""Persist the exact final native preview independently of publication consent."""

from copy import deepcopy

from django.db import transaction
from django.db.models import Exists, OuterRef

from .channel_artifacts import enabled
from .channel_artifact_contract import canonical_bytes
from .cms_owned_media import completed_media, ACTION as MEDIA_ACTION
from .cms_staged_article import build_staged_article, validate_staged_article
from .cms_api_transport import CmsApiError
from .website_contract import WebsiteAuthorityError

ACTION = "cms-stage"
FIELDS = {"schema_version", "binding", "credential_operation_id", "media_operation_id", "handoff_digest", "staged"}


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The final native CMS review, payment or connection changed.")


def _receipt(value):
    return {"status": "artifact_staged", "channel": "cms", "sourceRunId": value["source_run_id"],
        "reviewRevision": value["revision"], "qualityBasis": "cms_exact_media_substitution_v1",
        "provider_modified": False, "approval_recorded": False, "publishing_verified": False}


def start_stage(media_operation):
    """Seal only pre-reviewed bytes with mechanically verified media changes."""
    if not enabled():
        raise _deny()
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    source = completed_media(media_operation, fresh=False)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.run.organization_id)
        media = WebsiteConnectionOperation.objects.select_for_update().filter(pk=media_operation.pk, action=MEDIA_ACTION).first()
        source = completed_media(media, fresh=False)
        value = build_staged_article(source, media_intents=media.payload["intents"], media_states=media.payload["states"])
        private = {"schema_version": 1, "binding": source.admitted.scope.binding,
            "credential_operation_id": source.admitted.operation.payload["credential_operation_id"],
            "media_operation_id": str(media.pk), "handoff_digest": source.handoff["digest"], "staged": value}
        operation, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key="cms-stage:" + value["revision"],
            defaults={"action": ACTION, "connection": source.admitted.scope.connection,
                "generation": source.admitted.scope.binding["generation"], "state": "completed",
                "next_attempt_at": None, "payload": private, "receipt": _receipt(value)})
        if (operation.action != ACTION or operation.state != "completed" or
                operation.generation != source.admitted.scope.binding["generation"] or
                str(operation.connection_id) != source.admitted.scope.binding["connection_id"] or
                canonical_bytes(operation.payload) != canonical_bytes(private) or canonical_bytes(operation.receipt) != canonical_bytes(_receipt(value))):
            raise _deny()
        return operation


def current_stage(operation, *, fresh_media=True, now=None):
    """Resolve only private persisted source/proofs; never accept owner artifact JSON."""
    from .website_models import WebsiteConnectionOperation
    try:
        if (not enabled() or operation is None or operation.action != ACTION or operation.state != "completed" or
                not isinstance(operation.payload, dict) or set(operation.payload) != FIELDS or
                type(operation.payload["schema_version"]) is not int or operation.payload["schema_version"] != 1):
            raise _deny()
        payload = operation.payload
        media = WebsiteConnectionOperation.objects.filter(pk=payload["media_operation_id"], action=MEDIA_ACTION).first()
        source = completed_media(media, now=now, fresh=fresh_media)
        value = validate_staged_article(payload["staged"], source, media_intents=media.payload["intents"], media_states=media.payload["states"])
        if (payload["binding"] != source.admitted.scope.binding or payload["handoff_digest"] != source.handoff["digest"] or
                payload["credential_operation_id"] != source.admitted.operation.payload["credential_operation_id"] or
                operation.generation != payload["binding"]["generation"] or str(operation.connection_id) != payload["binding"]["connection_id"] or
                canonical_bytes(operation.receipt) != canonical_bytes(_receipt(value))):
            raise _deny()
        return source, deepcopy(value), media
    except (CmsApiError, WebsiteAuthorityError):
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def ensure_stages(*, limit=20, connection_id=None):
    """Finish completed media sets automatically, without HTTP or paid generation."""
    if not enabled():
        return 0
    from .website_models import WebsiteConnectionOperation
    existing = WebsiteConnectionOperation.objects.filter(action=ACTION, payload__handoff_digest=OuterRef("payload__handoff_digest"))
    media = WebsiteConnectionOperation.objects.filter(action=MEDIA_ACTION, state="completed", connection__state="connected")
    if connection_id:
        media = media.filter(connection_id=connection_id)
    media = media.annotate(_stage_exists=Exists(existing)).filter(_stage_exists=False)
    count = 0
    for row in media.order_by("created_at")[:limit]:
        try:
            start_stage(row)
            count += 1
        except (WebsiteAuthorityError, CmsApiError):
            continue
    return count
