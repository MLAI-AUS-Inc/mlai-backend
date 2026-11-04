"""At-most-once external brief suggestions for explicit keyed catalogue owners.

A committed claim precedes HTTP. Pending/unknown outcomes never expire and never
retry model work. Accepted results are small suggestions, not article content,
and remain usable only against the exact current catalogue they were based on.
"""
from copy import deepcopy
import hashlib
import json
import re
import time
from uuid import uuid4

from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response

from organizations.models import Organization
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from .catalog_commands import KEY, MAX_COMMANDS, CatalogCommandConflict, _digest, _receipts, _require_current_owner
from .editorial_catalog import CatalogConflict, article_brief_for_catalog, review_payload
from .models import OrganizationContentConfig

MAX_SUGGESTIONS = 32
MAX_RESPONSE_BYTES = 32768
READ_SECONDS = 15
_FIELDS = frozenset({"schema_version", "organization_id", "company_id", "user_id", "domain", "action", "body_sha256",
    "expected_version", "catalog_sha256", "nonce", "state", "created_at", "updated_at", "outcome", "response", "response_sha256"})
_OUTCOMES = {"claimed", "accepted", "remote_unavailable", "remote_refused", "invalid_response", "authority_changed"}


def valid_suggestion_receipt(value):
    """Validate only the closed private suggestion record, never repair it."""
    from uuid import UUID
    if not isinstance(value, dict) or set(value) != _FIELDS:
        return False
    try:
        company = str(UUID(value["company_id"]))
        nonce = str(UUID(value["nonce"]))
        times = [parse_datetime(value[k]) if isinstance(value[k], str) else None for k in ("created_at", "updated_at")]
        encoded = json.dumps(value["response"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, AttributeError):
        return False
    state, outcome = value["state"], value["outcome"]
    return bool(type(value["schema_version"]) is int and value["schema_version"] == 1
        and type(value["organization_id"]) is int and value["organization_id"] > 0
        and company == value["company_id"] and nonce == value["nonce"]
        and type(value["user_id"]) is int and value["user_id"] > 0
        and isinstance(value["domain"], str) and 0 < len(value["domain"]) <= 253
        and value["action"] == "brief_suggestion"
        and all(isinstance(value[k], str) and re.fullmatch(r"[0-9a-f]{64}", value[k]) for k in ("body_sha256", "catalog_sha256"))
        and type(value["expected_version"]) is int and value["expected_version"] >= 0
        and isinstance(state, str) and state in {"pending", "accepted", "held"}
        and isinstance(outcome, str) and outcome in _OUTCOMES
        and all(at is not None and timezone.is_aware(at) for at in times) and times[1] >= times[0]
        and ((state == "pending" and outcome == "claimed" and value["response"] is None and value["response_sha256"] is None)
             or (state == "held" and outcome not in {"claimed", "accepted"} and value["response"] is None and value["response_sha256"] is None)
             or (state == "accepted" and outcome == "accepted" and _response_shape(value["response"])
                 and value["response_sha256"] == hashlib.sha256(encoded).hexdigest()))
        and len(encoded) <= MAX_RESPONSE_BYTES)


def _response_shape(value):
    return bool(isinstance(value, dict) and set(value) == {"topic", "editorial_catalog_version", "brief", "rationale"}
        and isinstance(value["topic"], str) and 3 <= len(value["topic"]) <= 500
        and type(value["editorial_catalog_version"]) is int and value["editorial_catalog_version"] >= 0
        and isinstance(value["rationale"], str) and 1 <= len(value["rationale"]) <= 2000
        and (value["brief"] is None or isinstance(value["brief"], dict)))


def _validate_body(body):
    if not isinstance(body, dict) or set(body) - {"company_id", "topic", "country", "audience_id", "expected_editorial_catalog_version"}:
        raise ValueError("Supply the topic, country, audience and current catalogue revision only.")
    topic, country, version, audience = (body.get(k) for k in ("topic", "country", "expected_editorial_catalog_version", "audience_id"))
    if (not isinstance(topic, str) or not 3 <= len(topic.strip()) <= 500
            or not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country)
            or type(version) is not int or version < 0
            or audience is not None and (not isinstance(audience, str) or not 1 <= len(audience) <= 200)):
        raise ValueError("Supply a topic, country and current catalogue revision.")
    return topic.strip(), country, version, audience


def _locked(context, user_id):
    # The same lock order as local catalogue commands; HTTP is never inside it.
    from .editorial_views import _lock_catalog_owner
    org = Organization.objects.select_for_update().get(pk=context.organization.pk)
    _lock_catalog_owner(context, user_id, org)
    config = OrganizationContentConfig.objects.select_for_update().filter(organization=org).first()
    strategy = config.pillar_strategy if config else {}
    if not isinstance(strategy, dict):
        raise CatalogCommandConflict("Stored catalogue requests cannot be verified.")
    _require_current_owner(organization=org, context=context, user_id=user_id, strategy=strategy)
    return org, config, strategy


def _save(org, config, strategy):
    if config is None:
        OrganizationContentConfig.objects.create(organization=org, pillar_strategy=strategy)
    else:
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])


def _validate_response(value, body, strategy):
    if (not _response_shape(value) or value["topic"] != body["topic"].strip()
            or value["editorial_catalog_version"] != body["expected_editorial_catalog_version"]):
        raise ValueError("The suggestion response does not match this request.")
    if value["brief"] is not None:
        brief = article_brief_for_catalog(strategy, {"editorial_brief": value["brief"]})
        if brief != value["brief"] or brief["country"] != body["country"] or body.get("audience_id") not in (None, brief["audience_id"]):
            raise ValueError("The suggestion does not match the approved audience.")
    return value


def _read_response(result):
    started, data = time.monotonic(), bytearray()
    try:
        if result.status_code != 200:
            return None, "remote_refused"
        # requests iter_content delegates to raw.stream. A byte at a time makes
        # the elapsed deadline effective even against a slow trickle.
        for chunk in result.iter_content(chunk_size=1):
            if time.monotonic() - started > READ_SECONDS:
                raise ValueError("Suggestion response exceeded its read deadline.")
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Suggestion response is too large.")
        if time.monotonic() - started > READ_SECONDS:
            raise ValueError("Suggestion response exceeded its read deadline.")
        return json.loads(bytes(data).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON"))), "accepted"
    finally:
        result.close()


def suggestion_response(context, user_id, body, *, key, remote, headers):
    """Return a retained v2 suggestion response, or None for the legacy path."""
    import requests
    from .p2_foundation import p2_applies
    if key is None:
        return None
    stored = OrganizationContentConfig.objects.filter(organization=context.organization).first()
    initial = stored.pillar_strategy if stored else {}
    if not isinstance(initial, dict):
        return Response({"error": "Stored catalogue requests cannot be verified."}, status=409)
    if not p2_applies(context.organization) and KEY not in initial:
        return None
    try:
        topic, country, version, audience = _validate_body(body)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
            raise ValueError("A valid catalogue request key is required.")
        if connection.in_atomic_block:
            return Response({"error": "A suggestion must start outside an existing transaction."}, status=503)
        key_sha = hashlib.sha256(key.encode()).hexdigest()
        identity = {"schema_version": 1, "organization_id": context.organization.pk,
            "company_id": str(context.company.pk), "user_id": user_id, "domain": context.organization.domain,
            "action": "brief_suggestion", "body_sha256": _digest(body)}
        with transaction.atomic():
            org, config, strategy = _locked(context, user_id)
            records = _receipts(strategy)
            previous = records.get(key_sha)
            current = review_payload(strategy)
            if previous is not None:
                if previous["user_id"] != user_id:
                    raise PermissionError("This suggestion belongs to a different owner.")
                if any(previous[k] != v for k, v in identity.items()):
                    raise CatalogCommandConflict("This request key already records different details.")
                if previous["state"] != "accepted":
                    return Response({"error": "This suggestion's outcome is not confirmed. Enter the brief manually.", "code": "editorial_suggestion_pending"}, status=409)
                if current["editorial_catalog_version"] != previous["expected_version"] or _digest(current) != previous["catalog_sha256"]:
                    raise CatalogCommandConflict("The catalogue changed; request a new suggestion explicitly.")
                _validate_response(previous["response"], body, strategy)
                return Response(deepcopy(previous["response"]), headers={"Idempotent-Replayed": "true"})
            if not p2_applies(org):
                raise CatalogCommandConflict("New suggestion commands are currently unavailable.")
            if not remote["enabled"]:
                return Response({"error": "Brief suggestions are unavailable; enter the brief manually."}, status=503)
            if len(records) >= MAX_COMMANDS or sum(r["action"] == "brief_suggestion" for r in records.values()) >= MAX_SUGGESTIONS:
                raise CatalogCommandConflict("This catalogue has reached its saved suggestion limit.")
            if current["editorial_catalog_version"] != version:
                raise CatalogCommandConflict("Reload the current catalogue before requesting a suggestion.")
            nonce, now = str(uuid4()), timezone.now().isoformat()
            receipt = {**identity, "expected_version": version, "catalog_sha256": _digest(current), "nonce": nonce,
                "state": "pending", "created_at": now, "updated_at": now, "outcome": "claimed", "response": None, "response_sha256": None}
            _save(org, config, {**strategy, KEY: {**records, key_sha: receipt}})
        # The durable claim has committed; no implicit retry is allowed after it.
        response, outcome = None, "remote_unavailable"
        try:
            result = requests.post(remote["base_url"] + "/api/org/editorial-brief-suggestion", headers=headers,
                timeout=(5, 100), allow_redirects=False, stream=True, json={"domain": org.domain, "topic": topic,
                    "country": country, "expected_editorial_catalog_version": version, "audience_id": audience})
            response, outcome = _read_response(result)
            if outcome == "accepted":
                _validate_response(response, body, strategy)
        except requests.RequestException:
            outcome = "remote_unavailable"
        except (ValueError, TypeError):
            outcome = "invalid_response"
        with transaction.atomic():
            try:
                current_org, config, current_strategy = _locked(context, user_id)
            except (PermissionError, VibeRaisingProfile.DoesNotExist, VibeRaisingCompany.DoesNotExist, Organization.DoesNotExist):
                # The committed pending claim remains held; no result is exposed
                # and no new HTTP may follow this key after a role/tenant change.
                return Response({"error": "This startup is no longer writable."}, status=403)
            records = _receipts(current_strategy)
            retained = records.get(key_sha)
            if retained != receipt:
                raise CatalogCommandConflict("The saved suggestion claim changed; its outcome remains held.")
            if _digest(review_payload(current_strategy)) != receipt["catalog_sha256"]:
                outcome = "authority_changed"
            if outcome == "accepted":
                _validate_response(response, body, current_strategy)
            final = {**receipt, "state": "accepted" if outcome == "accepted" else "held", "outcome": outcome,
                "updated_at": timezone.now().isoformat(), "response": response if outcome == "accepted" else None,
                "response_sha256": _digest(response) if outcome == "accepted" else None}
            if not valid_suggestion_receipt(final):
                raise CatalogCommandConflict("The suggestion outcome cannot be verified.")
            _save(current_org, config, {**current_strategy, KEY: {**records, key_sha: final}})
        return Response(response) if outcome == "accepted" else Response({"error": "This suggestion's outcome is not confirmed. Enter the brief manually.", "code": "editorial_suggestion_pending"}, status=409)
    except (PermissionError, VibeRaisingProfile.DoesNotExist):
        return Response({"error": "Only the current active startup owner can request a suggestion."}, status=403)
    except (VibeRaisingCompany.DoesNotExist, Organization.DoesNotExist):
        return Response({"error": "Company configuration not found."}, status=404)
    except CatalogConflict as exc:
        return Response({"error": str(exc), "code": "editorial_catalog_conflict"}, status=409)
    except ValueError as exc:
        return Response({"error": str(exc), "code": "invalid_editorial_catalog"}, status=400)
