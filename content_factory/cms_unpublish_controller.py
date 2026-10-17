"""Leased status-only withdrawal; unknown writes recover by fixed-ID GET only."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_unpublish_authority as authority
from . import cms_unpublish_contract as contract
from . import cms_unpublish_public as public
from .cms_api_transport import CmsApiError, request_json
from .website_failure_contract import classify_failure

LEASE_SECONDS = 180


def _guard(identifier, lease, *, allowance=None):
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
    if candidate is None: raise authority.deny()
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
        if row is None: raise authority.deny()
        value = authority.current(row, lease_id=lease)
        if allowance is not None:
            maximum = {"provider_reads": 12, "public_reads": 8}[allowance]
            if row.payload[allowance] >= maximum: raise CmsApiError("post_recovery_unavailable")
            row.payload = {**deepcopy(row.payload), allowance: row.payload[allowance] + 1}
            row.save(update_fields=["payload", "updated_at"])
        return row, value


def _store(identifier, lease, *, expected, journal=None, issued=False, proof=None):
    """Recheck the winning journal and owner before every durable transition."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate, _value = _guard(identifier, lease)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
        if row is None: raise authority.deny()
        value = authority.current(row, lease_id=lease)
        if value.journal != expected: raise authority.deny()
        effect = None
        if issued:
            journal, effect = contract.issue(value.intent, value.journal,
                publication=value.original.intent, published_state=value.original.state)
        if journal is not None:
            contract.validate_state(journal, value.intent, publication=value.original.intent, published_state=value.original.state)
            row.payload = {**deepcopy(row.payload), "journal": journal}
        if proof is not None:
            public.validate_observation(proof, value.intent, row.payload["journal"],
                publication=value.original.intent, published_state=value.original.state, now=timezone.now())
            row.payload = {**deepcopy(row.payload), "public_observation": proof}
        row.save(update_fields=["payload", "updated_at"])
        return row, effect


def _request(identifier, lease, effect):
    if effect.method == "GET": _guard(identifier, lease, allowance="provider_reads")
    row, value = _guard(identifier, lease)
    if effect.method != "GET" and value.journal["phase"] != "issued": raise authority.deny()
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    result, _pagination = request_json(value.intent["binding"], value.credential, method=effect.method,
        resource="posts", identifier=effect.identifier, query=effect.query, payload=effect.payload)
    _guard(identifier, lease)
    return result


def _step(identifier, lease):
    _row, value = _guard(identifier, lease)
    if value.journal["phase"] == "reconcile_exhausted": raise CmsApiError("post_recovery_unavailable")
    effect = contract.read_effect(value.intent, value.journal, publication=value.original.intent, published_state=value.original.state)
    response = _request(identifier, lease, effect)
    _row, value = _guard(identifier, lease)
    observed = contract.observe(value.intent, value.journal, response, publication=value.original.intent, published_state=value.original.state)
    _store(identifier, lease, expected=value.journal, journal=observed)
    if observed["phase"] == "ready":
        _row, effect = _store(identifier, lease, expected=observed, issued=True)
        response = _request(identifier, lease, effect)
        _row, value = _guard(identifier, lease)
        observed = contract.observe(value.intent, value.journal, response, publication=value.original.intent, published_state=value.original.state)
        _store(identifier, lease, expected=value.journal, journal=observed)
        # Provider draft and public absence are independently observed on the
        # next lease. No response to a write alone establishes public removal.
        return
    if observed["phase"] == "provider_draft":
        _guard(identifier, lease, allowance="public_reads")
        _row, value = _guard(identifier, lease)
        from .website_connections import require_unlocked_remote_call
        require_unlocked_remote_call()
        status = public.fetch_absence(value.intent, value.journal,
            publication=value.original.intent, published_state=value.original.state)
        _guard(identifier, lease)
        proof = public.observation(value.intent, value.journal, status,
            publication=value.original.intent, published_state=value.original.state, checked_at=timezone.now().isoformat())
        _store(identifier, lease, expected=value.journal, proof=proof)


def _finish(identifier, lease, *, failure=None):
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate, _value = _guard(identifier, lease)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
        if row is None: raise authority.deny()
        value, now = authority.current(row, lease_id=lease), timezone.now()
        row.payload = deepcopy(row.payload)
        phase = value.journal["phase"]
        if row.payload["public_observation"] is not None:
            row.state, row.next_attempt_at = "completed", None
            row.receipt = {"status": "article_withdrawn", "channel": "cms", "provider_modified": value.journal["issue_count"] == 1,
                "publishing_verified": False, "withdrawal_verified": True,
                "publicProofDigest": row.payload["public_observation"]["digest"]}
        else:
            waiting = failure in {None, "public_article_unverified", "public_article_unavailable"}
            if failure is not None and not waiting:
                row.payload["failures"] = min(20, row.payload["failures"] + 1)
            repeated = failure is not None and not waiting and row.payload["last_failure_code"] == failure
            if failure is not None and not waiting: row.payload["last_failure_code"] = failure
            exhausted = phase == "reconcile_exhausted" or failure == "post_recovery_unavailable"
            bad_key = failure == "credentials_required"
            retry = not (exhausted or bad_key or repeated or row.payload["failures"] >= 20)
            code = "missing_or_expired_credentials" if bad_key else "CONNECTION_SERVICE_TRANSIENT" if retry else "PUBLISHING_ADAPTER_REQUIRED"
            classified = classify_failure(code, "cms_unpublish")
            row.state, row.next_attempt_at = ("pending", min(now + timedelta(seconds=30), value.deadline)) if retry else (
                "needs_user" if bad_key else "blocked", None)
            row.receipt = {"status": "withdrawal_pending" if retry else "withdrawal_access_required" if bad_key else "withdrawal_stopped",
                "channel": "cms", "provider_modified": None if value.journal["outcome_unknown"] else value.journal["issue_count"] == 1,
                "outcome_unknown": value.journal["outcome_unknown"], "publishing_verified": False, "failure": classified,
                "user_action": {"id": "connect_cms" if bad_key else "inspect_goal"} if not retry else None}
        row.payload.pop("lease_id", None)
        row.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
        return row


def advance(identifier, *, now=None):
    """Take one bounded private lease; no HTTP or finance runs with rollout off."""
    if not authority.enabled(): return None
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
    if candidate is None or candidate.state not in authority.ACTIVE: return candidate
    now, lease, claimed = now or timezone.now(), str(uuid4()), False
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
            if row is None or row.state not in authority.ACTIVE or row.next_attempt_at and row.next_attempt_at > now: return row
            authority.current(row, now=now)
            if row.state == "running":
                repeated = row.payload["last_failure_code"] == "worker_lost"
                row.payload = {**deepcopy(row.payload), "failures": min(20, row.payload["failures"] + 1), "last_failure_code": "worker_lost"}
                if repeated:
                    row.payload.pop("lease_id", None)
                    row.state, row.next_attempt_at = "blocked", None
                    row.receipt = {"status": "withdrawal_worker_stopped", "channel": "cms", "publishing_verified": False,
                        "failure": classify_failure("RUN_STALLED_WORKER_LOST", "cms_unpublish"),
                        "user_action": {"id": "inspect_goal"}, "outcome_unknown": row.payload["journal"]["outcome_unknown"]}
                    row.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])
                    return row
            row.payload = {**deepcopy(row.payload), "lease_id": lease}
            row.state, row.next_attempt_at = "running", min(now + timedelta(seconds=LEASE_SECONDS), authority.historical(row).deadline)
            row.attempts += 1
            row.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
            claimed = True
        try:
            _step(identifier, lease)
            failure = None
        except CmsApiError as exc:
            failure = exc.category
        return _finish(identifier, lease, failure=failure)
    except (authority.WebsiteAuthorityError, CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        # A late reply cannot cancel a newer lease, reset an issued journal or
        # erase a possible write. Expired/revoked work is visibly stopped.
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"state": candidate.state, "updated_at": candidate.updated_at}
        row = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION, **owned).first()
        if row is not None:
            payload = {key: value for key, value in row.payload.items() if key != "lease_id"}
            expired = False
            try: expired = authority.historical(row).deadline <= timezone.now()
            except (authority.WebsiteAuthorityError, CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError): pass
            WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION, **owned).update(payload=payload,
                state="blocked", next_attempt_at=None, receipt={"status": "withdrawal_deadline_reached" if expired else "withdrawal_authority_changed",
                    "channel": "cms", "publishing_verified": False, "provider_modified": None,
                    "outcome_unknown": True, "user_action": {"id": "inspect_goal"}}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
