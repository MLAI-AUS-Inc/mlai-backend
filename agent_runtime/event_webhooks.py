"""OpenAI MCP Events webhook extension (2026-07-28), opt-in.

Subscriptions use existing durable records. Callback secrets are encrypted,
never included in tools, task inputs, audit metadata or worker credentials.
There is no replay promise in this extension; portable cursor reads are separate.
"""

import base64
from datetime import timedelta
import hashlib
import hmac
import json
import secrets
import uuid

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .authentication import validate_credential
from .events import event_payload, should_wake
from .models import AgentCredential, AgentEvent, AgentSubscription
from .policy import AgentPermissionDenied, authorize, bounded_json
from .runtime import _grant, locked, audit
from .sources import live_access
from .webhook_transport import CallbackEndpointError, post_webhook, resolve_callback

EVENT_NAMES = ("message.created", "message.updated", "message.deleted", "membership.changed")
MAX_TTL_MS = 24 * 60 * 60 * 1000
MAX_SUBSCRIPTIONS_PER_GRANT = 100


def enabled():
    return bool(getattr(settings, "AGENT_RUNTIME_ENABLED", False) and getattr(settings, "AGENT_RUNTIME_MCP_EVENTS_ENABLED", False))


def _cipher():
    # Domain-separated from event cursors and account tokens; only the trusted
    # backend has the Django secret. Rotating it intentionally requires reconnect.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("mlai-webhook-secrets-v1:" + settings.SECRET_KEY).encode()).digest()))


def seal(config):
    return _cipher().encrypt(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).decode()


def unseal(value):
    try:
        result = json.loads(_cipher().decrypt(value.encode()))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise CallbackEndpointError("subscription_credentials_unavailable") from None


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 120:
        raise CallbackEndpointError("invalid_signing_secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except ValueError:
        raise CallbackEndpointError("invalid_signing_secret") from None
    if not 24 <= len(key) <= 64:
        raise CallbackEndpointError("invalid_signing_secret")
    return key


def signed_headers(subscription_id, event_id, body, secret, *, now=None, old_secret=None):
    stamp = str(int((now or timezone.now()).timestamp()))
    material = event_id.encode() + b"." + stamp.encode() + b"." + body
    signatures = ["v1," + base64.b64encode(hmac.new(signing_key(key), material, hashlib.sha256).digest()).decode()
                  for key in (secret, old_secret) if key]
    return {"webhook-id": event_id, "webhook-timestamp": stamp,
            "webhook-signature": " ".join(signatures), "X-MCP-Subscription-Id": str(subscription_id)}


def _body(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def verify_callback(subscription_id, url, secret, *, transport=post_webhook, now=None):
    now = now or timezone.now()
    challenge = secrets.token_urlsafe(32)
    body = _body({"type": "verification", "challenge": challenge})
    response = transport(url, body, signed_headers(subscription_id, "verify_" + uuid.uuid4().hex, body, secret, now=now))
    try:
        answer = json.loads(response.body).get("challenge")
    except (ValueError, AttributeError):
        answer = None
    if (not 200 <= response.status < 300 or not isinstance(answer, str)
            or not hmac.compare_digest(challenge, answer)
            or timezone.now() > now + timedelta(seconds=30)):
        raise CallbackEndpointError("challenge_failed")


def identity(grant, params, *, subscribing=True):
    allowed = {"name", "arguments", "delivery"} | ({"cursor", "ttlMs"} if subscribing else set())
    if not isinstance(params, dict) or set(params) - allowed or params.get("name") not in EVENT_NAMES:
        raise AgentPermissionDenied("invalid_subscription")
    arguments, delivery = params.get("arguments"), params.get("delivery")
    if (not isinstance(arguments, dict) or set(arguments) != {"resource"}
            or not isinstance(delivery, dict) or delivery.get("mode") != "webhook"
            or set(delivery) != ({"mode", "url", "secret"} if subscribing else {"mode", "url"})):
        raise AgentPermissionDenied("invalid_subscription")
    resource, url = arguments["resource"], delivery["url"]
    if not isinstance(resource, str) or not isinstance(url, str):
        raise AgentPermissionDenied("invalid_subscription")
    subscription_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
        ["mlai-mcp-events-v1", str(grant.pk), url, params["name"], arguments], sort_keys=True, separators=(",", ":")))
    return subscription_id, resource, url


def _authorized(principal, resource):
    grant = _grant(principal.grant.pk)
    credential = locked(AgentCredential.objects.all()).select_related("task").get(pk=principal.credential.pk)
    credential.grant = grant
    current = validate_credential(credential, audience=principal.credential.audience, allowed_kinds=("access",))
    if "events:read" not in credential.scopes:
        raise AgentPermissionDenied("scope_denied")
    authorize(grant, "events:read", resource, expected_generation=credential.generation, access_checker=live_access)
    return current


def event_definitions(principal):
    if "events:read" not in principal.credential.scopes:
        return {"events": []}
    permitted = []
    with transaction.atomic():
        for resource in principal.grant.resources:
            try:
                _authorized(principal, resource)
                permitted.append(resource)
            except AgentPermissionDenied:
                continue
    if not permitted:
        return {"events": []}
    return {"events": [{"name": name, "description": "A change in a selected community channel. Fetch content through authorized read tools.",
            "delivery": ["webhook"], "inputSchema": {"type": "object", "properties": {
                "resource": {"type": "string", "enum": permitted}}, "required": ["resource"], "additionalProperties": False},
            "payloadSchema": {"type": "object", "properties": {"resource": {"type": "string"}, "identifiers": {"type": "object"}},
                              "required": ["resource", "identifiers"], "additionalProperties": False}}
            for name in EVENT_NAMES]}


def subscribe(principal, params, *, now=None, transport=post_webhook):
    now = now or timezone.now()
    subscription_id, resource, url = identity(principal.grant, params)
    if params.get("cursor") is not None:
        raise AgentPermissionDenied("webhook_replay_not_supported")
    ttl = params.get("ttlMs", MAX_TTL_MS)
    if ttl is None:
        ttl = MAX_TTL_MS
    if type(ttl) is not int or ttl <= 0:
        raise AgentPermissionDenied("invalid_subscription_lifetime")
    secret = params["delivery"]["secret"]
    signing_key(secret)
    # Validation and connection-time validation are both intentional. The
    # transport pins its own freshly validated resolution before sending.
    with transaction.atomic():
        current = _authorized(principal, resource)
        grant = current.grant
        resolve_callback(url)
        existing = locked(AgentSubscription.objects.all()).filter(pk=subscription_id, grant=grant).first()
        if not existing or not existing.is_active:
            active_count = AgentSubscription.objects.filter(grant=grant, is_active=True, delivery__mode="webhook").count()
            if active_count >= MAX_SUBSCRIPTIONS_PER_GRANT:
                raise AgentPermissionDenied("subscription_limit_reached")
        old = unseal(existing.delivery.get("sealed", "")) if existing and existing.is_active else {}
        verified_at = parse_datetime(old.get("verified_at", "")) if old else None
        if not (verified_at and verified_at > now - timedelta(minutes=5) and old.get("url") == url and old.get("secret") == secret):
            verify_callback(subscription_id, url, secret, transport=transport, now=now)
        expires = min(grant.expires_at, now + timedelta(milliseconds=min(ttl, MAX_TTL_MS)))
        config = {"url": url, "secret": secret, "verified_at": now.isoformat()}
        if old.get("secret") and old["secret"] != secret:
            config.update(old_secret=old["secret"], old_until=(now + timedelta(minutes=5)).isoformat())
        # New subscriptions start at the latest committed event of this
        # resource, under the same serialization boundary used by ingestion.
        from .events import lock_event_resource
        lock_event_resource(resource)
        old_expiry = parse_datetime(existing.delivery.get("expires_at", "")) if existing else None
        continuing = existing and existing.is_active and old_expiry and old_expiry > now
        cursor = existing.cursor if continuing else (AgentEvent.objects.filter(resource=resource).order_by("-pk").values_list("pk", flat=True).first() or 0)
        delivery = {"mode": "webhook", "sealed": seal(config), "expires_at": expires.isoformat(), "attempts": 0}
        AgentSubscription.objects.update_or_create(pk=subscription_id, defaults={
            "grant": grant, "generation": grant.generation, "resource": resource,
            "event_types": [params["name"]], "cursor": cursor, "instruction": "", "is_active": True,
            "allow_agent_events": False, "delivery": delivery,
        })
        audit("webhook_subscribed", grant=grant, subscription_id=str(subscription_id))
    return {"id": str(subscription_id), "refreshBefore": expires.isoformat(), "cursor": None, "truncated": False}


def unsubscribe(principal, params):
    subscription_id, resource, _url = identity(principal.grant, params, subscribing=False)
    with transaction.atomic():
        current = _authorized(principal, resource)
        AgentSubscription.objects.filter(pk=subscription_id, grant=current.grant).update(is_active=False, delivery={"mode": "webhook", "stopped": True})
        audit("webhook_unsubscribed", grant=current.grant, subscription_id=str(subscription_id))
    return {}


def handle_method(principal, method, params):
    if not enabled():
        raise AgentPermissionDenied("mcp_events_disabled")
    if method == "events/list":
        if params not in ({}, {"cursor": None}):
            raise AgentPermissionDenied("invalid_event_catalog_request")
        return event_definitions(principal)
    if method == "events/subscribe":
        return subscribe(principal, params)
    if method == "events/unsubscribe":
        return unsubscribe(principal, params)
    raise AgentPermissionDenied("unknown_event_method")


def dispatch_webhooks(*, limit=50, now=None, transport=post_webhook):
    if not enabled():
        return 0
    supplied_now = now
    sent = 0
    ids = list(AgentSubscription.objects.filter(is_active=True, delivery__mode="webhook")
               .order_by("updated_at").values_list("pk", flat=True)[:min(100, max(1, limit))])
    for subscription_id in ids:
        # A full bounded batch may take minutes. Re-evaluate each subscription's
        # lifetime at its own delivery boundary instead of reusing batch start.
        now = supplied_now or timezone.now()
        with transaction.atomic():
            grant_id = AgentSubscription.objects.values_list("grant_id", flat=True).get(pk=subscription_id)
            grant = _grant(grant_id)
            subscription = locked(AgentSubscription.objects.all()).get(pk=subscription_id)
            if not subscription.is_active:
                continue
            delivery = dict(subscription.delivery)
            expires = parse_datetime(delivery.get("expires_at", ""))
            try:
                authorize(grant, "events:read", subscription.resource, expected_generation=subscription.generation, access_checker=live_access)
                if not expires or expires <= now:
                    raise AgentPermissionDenied("subscription_expired")
                config = unseal(delivery["sealed"])
            except (AgentPermissionDenied, CallbackEndpointError, KeyError) as exc:
                if isinstance(exc, AgentPermissionDenied) and exc.code == "resource_unavailable":
                    subscription.save(update_fields=["updated_at"])
                    continue
                subscription.is_active = False
                subscription.delivery = {"mode": "webhook", "stopped": True}
                subscription.save(update_fields=["is_active", "delivery", "updated_at"])
                continue
            retry_at = parse_datetime(delivery.get("retry_at", ""))
            if retry_at and retry_at > now:
                subscription.save(update_fields=["updated_at"])
                continue
            # One bounded event per subscription each tick. Keeping the cursor
            # unchanged on an uncertain request makes retries use the same ID.
            pending = AgentEvent.objects.filter(resource=subscription.resource, pk__gt=subscription.cursor).order_by("pk")[:50]
            for event in pending:
                if not should_wake(subscription, event):
                    subscription.cursor = event.pk
                    continue
                public = event_payload(event, grant)
                envelope = {"eventId": public["id"], "name": event.event_type,
                    "timestamp": event.occurred_at.isoformat(), "data": {
                        "resource": subscription.resource, "identifiers": bounded_json(event.payload, max_bytes=16384)}, "cursor": None}
                body = _body(envelope)
                old_until = parse_datetime(config.get("old_until", ""))
                old_secret = config.get("old_secret") if old_until and old_until > now else None
                headers = signed_headers(subscription.pk, envelope["eventId"], body, config["secret"], now=now, old_secret=old_secret)
                try:
                    response = transport(config["url"], body, headers)
                    accepted = 200 <= response.status < 300
                    permanent = response.status in {410, 413} or 400 <= response.status < 500 and response.status not in {408, 429}
                    reason = f"http_{response.status}"
                except CallbackEndpointError as exc:
                    accepted, permanent, reason = False, False, exc.reason
                if accepted:
                    subscription.cursor = event.pk
                    delivery.update(attempts=0)
                    delivery.pop("retry_at", None)
                    sent += 1
                else:
                    attempts = int(delivery.get("attempts", 0)) + 1
                    delivery.update(attempts=attempts, last_error=reason,
                                    retry_at=(now + timedelta(seconds=min(300, 5 * 2 ** min(attempts, 8)))).isoformat())
                    if permanent or attempts >= 8:
                        subscription.is_active = False
                break
            subscription.delivery = delivery
            subscription.save(update_fields=["cursor", "delivery", "is_active", "updated_at"])
    return sent
