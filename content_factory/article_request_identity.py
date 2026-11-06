"""Keep server-generated article metadata stable across a client retry."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from core.actor_ids import actor_ids_for_user

from .dispatch_models import ContentFactoryDispatchOutbox
from .website_contract import WebsiteAuthorityError


def stabilize_article_request_metadata(payload, *, organization, user):
    if "analytics_article_id" not in payload:
        return
    key = payload["client_request_id"]
    analytics_id = str(uuid5(NAMESPACE_URL, f"mlai:article:{organization.pk}:{key}"))
    row = ContentFactoryDispatchOutbox.objects.filter(
        organization=organization, client_request_id=key,
    ).first()
    if row is not None:
        envelope = row.payload if isinstance(row.payload, dict) else {}
        saved = envelope.get("request")
        if not isinstance(saved, dict):
            raise WebsiteAuthorityError("dispatch_key_conflict", "The original article request requires review.")
        actors = actor_ids_for_user(user)
        requester = saved.get("roo_points_requested_by_user_id")
        same_requester = str(requester) == str(user.pk) if requester is not None else saved.get("slack_user_id") in actors
        if not same_requester:
            raise WebsiteAuthorityError("dispatch_key_conflict", "This article request belongs to another account.")
        # Metadata is recovered only after matching the original tenant and
        # requester. Article content, target, source and consent stay current
        # inputs to the ordinary strict operation/outbox digest checks.
        analytics_id = saved.get("analytics_article_id") or analytics_id
        if isinstance(saved.get("analytics_config"), dict):
            payload["analytics_config"] = deepcopy(saved["analytics_config"])
        for field in ("slack_user_id", "requested_by_slack_user_id"):
            if field in payload and saved.get(field) in actors:
                payload[field] = saved[field]
    payload["analytics_article_id"] = analytics_id
    if isinstance(payload.get("analytics_config"), dict):
        payload["analytics_config"] = {**payload["analytics_config"], "analytics_article_id": analytics_id}
