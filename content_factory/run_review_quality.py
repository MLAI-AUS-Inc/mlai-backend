"""Current review authority is distinct from historically delivered article value."""
from copy import deepcopy
import hashlib

from .run_artifact_observations import _digest, _hash, _timestamp
from .run_ownership import CURRENT_REVIEW_RESULT_KEY, current_execution_ticket, owner_receipt, wire_identity


def metadata_digest(result):
    return _digest({"generation": result.get("generation"),
        "article_preview_quality": result.get("article_preview_quality"), "livePreview": result.get("livePreview")})


def current_review_verified(run):
    """A fresh ticket/body/quality proof is required for approval-purpose reads."""
    result = getattr(run, "result", None) or {}
    if not isinstance(result, dict):
        return False
    receipt = result.get(CURRENT_REVIEW_RESULT_KEY)
    owner = owner_receipt(run)
    try:
        export = result.get("articleExport") or {}
        body = export.get("markdown", "").replace("\r\n", "\n").replace("\r", "\n").strip()
        return (isinstance(receipt, dict) and owner is not None
            and type(receipt.get("schema_version")) is int and receipt["schema_version"] == 1
            and receipt.get("admission_digest") == _digest(wire_identity(owner))
            and receipt.get("execution") == current_execution_ticket(run)
            and export.get("status") == "ready" and receipt.get("revision") == export.get("revision")
            and receipt.get("article_sha256") == hashlib.sha256(body.encode()).hexdigest()
            and receipt.get("metadata_sha256") == metadata_digest(result)
            and all(_hash(receipt.get(key)) for key in ("evidence_sha256", "canonical_metadata_sha256"))
            and _timestamp(receipt.get("verified_at")))
    except (AttributeError, TypeError, ValueError, KeyError, UnicodeError, RecursionError):
        return False


def review_projection(run, record, metadata, body):
    """Match typed creation bytes to an independent closed current metadata read."""
    from .run_artifact_observations import ArtifactObservationInvalid
    from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, require_event_execution, require_event_identity
    from .vibe_marketing_views import _normalize_live_preview_payload
    fields = {"schema_version", "run_id", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, "generation", "revision",
              "article_sha256", "article_preview_quality", "livePreview"}
    if (not isinstance(metadata, dict) or set(metadata) != fields
            or type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1
            or metadata.get("run_id") != run.run_id or body is None):
        raise ArtifactObservationInvalid("The current article review metadata is unavailable.")
    require_event_identity(run, metadata)
    require_event_execution(run, metadata, current=True)
    require_event_execution(run, record, current=True)
    payload, ticket = record["payload"], current_execution_ticket(run)
    body_hash = hashlib.sha256(body["markdown"].replace("\r\n", "\n").replace("\r", "\n").strip().encode()).hexdigest()
    if (type(metadata.get("generation")) is not int or metadata["generation"] != ticket["generation"]
            or payload["generation"] != ticket["generation"]
            or metadata.get("revision") != payload["revision"] or body.get("revision") != payload["revision"]
            or metadata.get("article_sha256") != payload["article_sha256"] or body_hash != payload["article_sha256"]
            or metadata.get("article_preview_quality") != payload["article_preview_quality"]
            or metadata.get("livePreview") != payload["live_preview"]):
        raise ArtifactObservationInvalid("The typed quality differs from the current canonical review.")
    projection = {"generation": ticket["generation"],
        "article_preview_quality": deepcopy(payload["article_preview_quality"]),
        "livePreview": _normalize_live_preview_payload(deepcopy(payload["live_preview"])) or {}}
    return projection, _digest(metadata)


def verified_review_quality(run):
    """Current verified quality may inform offers without granting consent."""
    from .run_ownership import user_stop_intent
    if not current_review_verified(run) or user_stop_intent(run):
        return False
    from .run_execution_control import require_unsettled_execution
    try:
        require_unsettled_execution(run)
    except ValueError:
        return False
    from .article_publish_approval import article_review_identity_is_complete
    return article_review_identity_is_complete(run)
