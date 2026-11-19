"""A21: the not-on-GitHub exit leaves a lead row and one funnel event.

``POST /api/v1/my-startup/founder-tools/website-lead/`` (and the same path
under ``/api/v1/founder-tools/``) records that a founder's website code is not
on GitHub, before any startup exists, so the exit is counted and a later
reminder can reach them. It rides the GitHub-first gate
(``ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE``): with the gate off it
answers 404 and records nothing.

Storage: there is no lead model, and this step adds no migration. The lead is
kept on the founder's CRM ``Contact`` (the existing person record, keyed by the
account email) under ``attributes[LEADS_KEY]``, keyed by reason and domain.
Writing it never changes the contact's consent, status or owner. A dedicated
lead table is the follow-up (E3, E3b).

Nothing here fetches the domain: it is normalised text only.
"""
from __future__ import annotations

from datetime import timedelta
import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

LEADS_KEY = "founder_website_leads"
MAX_LEADS = 10
REMIND_AFTER = timedelta(days=7)
REASONS = frozenset({"not_on_github", "other_git_host", "not_in_git", "someone_else_holds_code", "no_github_account"})
DEFAULT_REASON = "not_on_github"
HOST_CLASS = re.compile(r"[a-z][a-z0-9_]{0,39}")
HOSTNAME = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
MESSAGE = "MLAI can only set up articles for websites whose code is on GitHub today."
NOTIFY_MESSAGE = "Thanks. We'll email you when that changes."
# Nothing durable holds the request, so nothing would send that email.
NOT_STORED_MESSAGE = "We couldn't save a reminder for this account, so we can't email you about it yet."
NOT_ENABLED = {"detail": "Not available.", "reason": "not_enabled"}


def lead_exit_enabled() -> bool:
    """The exit rides the GitHub-first gate: it records leads only while the gate is on."""
    return bool(getattr(settings, "ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE", False))


def _text(data, *names):
    for name in names:
        value = data.get(name) if hasattr(data, "get") else None
        if value is not None:
            return value
    return None


def normalise_lead_domain(value) -> str:
    """A bare lowercase hostname, or '' for anything that is not one. Never resolved."""
    from integrations.utils import normalize_domain
    text = str(value or "").strip()[:300]
    if not text:
        return ""
    try:
        domain = normalize_domain(text)
    except Exception:  # noqa: BLE001 - malformed input is simply not a domain
        return ""
    domain = str(domain or "").strip().lower().rstrip(".")
    return domain if HOSTNAME.fullmatch(domain) else ""


def parse_lead(data) -> dict:
    """Validate the request body. Raises ValueError with founder copy."""
    data = data if hasattr(data, "get") else {}
    reason = str(_text(data, "reason") or DEFAULT_REASON).strip()
    if reason not in REASONS:
        raise ValueError("Choose why the code isn't on GitHub.")
    host_class = str(_text(data, "hostClass", "host_class") or "unknown").strip().lower()
    if not HOST_CLASS.fullmatch(host_class):
        host_class = "unknown"
    notify = _text(data, "notifyWhenChanged", "notify_when_changed")
    if notify is not None and not isinstance(notify, bool):
        raise ValueError("notifyWhenChanged must be true or false.")
    return {"reason": reason, "domain": normalise_lead_domain(_text(data, "domain", "website")),
            "hostClass": host_class, "notify": bool(notify)}


def _contact_for(user, *, now):
    """The founder's CRM contact, created for a verified account email; None when not ours to write."""
    from crm.emails import normalize_contact_email
    from crm.models import Contact
    email = normalize_contact_email(getattr(user, "email", ""))
    if not email or not getattr(user, "email_verified_at", None):
        return None
    contact, _ = Contact.objects.select_for_update().get_or_create(
        email=email, defaults={"source": Contact.Source.CHAT_ACCOUNT, "first_seen_at": now, "user_id": user.pk})
    if contact.user_id not in (None, user.pk):
        return None
    return contact


def record_lead(user, lead: dict, *, now=None):
    """Upsert one lead for ``user``. Returns (public lead, created, stored)."""
    now = now or timezone.now()
    key = f"{lead['reason']}:{lead['domain']}"
    entry = {"version": 1, "userId": user.pk, "reason": lead["reason"], "domain": lead["domain"] or None,
             "hostClass": lead["hostClass"], "source": "my_startup.website_step"}
    with transaction.atomic():
        contact = _contact_for(user, now=now)
        if contact is None:
            # Nothing durable to dedupe against: a short-lived marker keeps the
            # funnel event to one per account, reason and domain.
            from django.core.cache import cache
            created = bool(cache.add(f"founder-website-lead:{user.pk}:{key}", 1, timeout=30 * 86400))
            stored = False
            # No reminder can reach an unstored lead, so none is promised.
            entry.update(createdAt=now.isoformat(), updatedAt=now.isoformat(), notifyWhenChanged=False, remindAt=None)
        else:
            attributes = dict(contact.attributes) if isinstance(contact.attributes, dict) else {}
            leads = dict(attributes.get(LEADS_KEY)) if isinstance(attributes.get(LEADS_KEY), dict) else {}
            previous = leads.get(key) if isinstance(leads.get(key), dict) else None
            created, stored = previous is None, True
            # Asking to be told is sticky: a later plain visit, or a later
            # notifyWhenChanged false, never withdraws it.
            notify = bool(lead["notify"] or (previous or {}).get("notifyWhenChanged") is True)
            entry.update(createdAt=(previous or {}).get("createdAt") or now.isoformat(), updatedAt=now.isoformat(),
                         notifyWhenChanged=notify,
                         remindAt=((previous or {}).get("remindAt") or (now + REMIND_AFTER).isoformat()) if notify else None)
            leads[key] = entry
            if len(leads) > MAX_LEADS:
                for stale in sorted(leads, key=lambda name: str(leads[name].get("updatedAt") or ""))[:len(leads) - MAX_LEADS]:
                    leads.pop(stale, None)
            attributes[LEADS_KEY] = leads
            contact.attributes = attributes
            fields = ["attributes", "updated_at"]
            if contact.user_id is None:
                contact.user_id = user.pk
                fields.append("user")
            contact.save(update_fields=fields)
        if created:
            from content_factory.founder_funnel import emit_event
            at, signed_up = now, getattr(user, "date_joined", None)
            transaction.on_commit(lambda: _emit(emit_event, user.pk, at, signed_up))
    if not stored:
        logger.info("founder_website_lead_not_stored user=%s reason=%s", user.pk, lead["reason"])
    public = {key: entry[key] for key in ("reason", "domain", "hostClass", "notifyWhenChanged", "remindAt", "createdAt")}
    return public, created, stored


def _emit(emit_event, user_id, at, signed_up):
    try:
        emit_event("not_on_github", user_id=user_id, at=at, signed_up_at=signed_up, source="website_lead.recorded")
    except Exception:  # noqa: BLE001 - telemetry never breaks the founder's action
        logger.exception("founder_website_lead_event_failed")


class FounderWebsiteLeadView(APIView):
    """Record the not-on-GitHub exit ("Tell me when this changes" sets notifyWhenChanged)."""

    def post(self, request):
        if not lead_exit_enabled():
            return Response(dict(NOT_ENABLED), status=status.HTTP_404_NOT_FOUND)
        try:
            lead = parse_lead(request.data)
        except ValueError as exc:
            return Response({"detail": str(exc), "reason": "invalid_lead"}, status=status.HTTP_400_BAD_REQUEST)
        public, created, stored = record_lead(request.user, lead)
        # "We'll email you" only for a request this server actually keeps.
        if stored and public["notifyWhenChanged"]:
            message = f"{MESSAGE} {NOTIFY_MESSAGE}"
        elif not stored and lead["notify"]:
            message = f"{MESSAGE} {NOT_STORED_MESSAGE}"
        else:
            message = MESSAGE
        return Response({"lead": public, "recorded": stored, "message": message},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
