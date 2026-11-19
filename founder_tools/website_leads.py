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
Writing it never changes the contact's consent, status or owner.

Plan E3: one not-on-GitHub exit is one lead row. Refining it on the host-class
screens (another reason from ``REASONS``, the builder the founder picked)
rewrites that row in place and never emits a second funnel event. The row
carries the guidance id and the state that guidance was in when the founder
saw it (``guidanceState``), so the lead reminders (E3b,
``website_lead_reminders``) can tell the founder when it changes.

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
# Plan C15: "Tell me when this changes" on a site whose articles live in a CMS
# or database. It shares the lead row, not the not-on-GitHub funnel event, and
# it is not gated on the GitHub-first switch (the stop happens either way).
INTEREST_REASONS = frozenset({"cms_content"})
CMS_MESSAGE = "MLAI can't add articles to a website whose posts live in a content system yet."
DEFAULT_REASON = "not_on_github"
HOST_CLASS = re.compile(r"[a-z][a-z0-9_]{0,39}")
GUIDANCE_ID = HOST_CLASS
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
    if reason not in REASONS | INTEREST_REASONS:
        raise ValueError("Choose why the code isn't on GitHub.")
    host_class = str(_text(data, "hostClass", "host_class") or "unknown").strip().lower()
    if not HOST_CLASS.fullmatch(host_class):
        host_class = "unknown"
    guidance_id = str(_text(data, "guidanceId", "guidance_id") or "").strip().lower()
    if not GUIDANCE_ID.fullmatch(guidance_id):
        guidance_id = ""
    notify = _text(data, "notifyWhenChanged", "notify_when_changed")
    if notify is not None and not isinstance(notify, bool):
        raise ValueError("notifyWhenChanged must be true or false.")
    return {"reason": reason, "domain": normalise_lead_domain(_text(data, "domain", "website")),
            "hostClass": host_class, "guidanceId": guidance_id or None, "notify": bool(notify)}


def _exit_keys(leads, domain):
    """Keys of the not-on-GitHub exit rows already kept for this domain (plan E3: one row per exit)."""
    return [name for name, value in leads.items()
            if isinstance(value, dict) and value.get("reason") in REASONS and (value.get("domain") or "") == domain]


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


def record_lead(user, lead: dict, *, now=None, source="my_startup.website_step"):
    """Upsert one lead for ``user``. Returns (public lead, created, stored).

    Only a not-on-GitHub reason (``REASONS``) emits the funnel's
    ``not_on_github`` event; other lead kinds share the row, not the event.
    """
    from .website_lead_reminders import guidance_state
    now = now or timezone.now()
    key = f"{lead['reason']}:{lead['domain']}"
    guidance_id = lead.get("guidanceId") or None
    entry = {"version": 1, "userId": user.pk, "reason": lead["reason"], "domain": lead["domain"] or None,
             "hostClass": lead["hostClass"], "guidanceId": guidance_id, "source": source}
    with transaction.atomic():
        contact = _contact_for(user, now=now)
        if contact is None:
            # Nothing durable to dedupe against: a short-lived marker keeps the
            # funnel event to one per account, reason and domain.
            from django.core.cache import cache
            marker = f"exit:{lead['domain']}" if lead["reason"] in REASONS else key
            created = bool(cache.add(f"founder-website-lead:{user.pk}:{marker}", 1, timeout=30 * 86400))
            stored = False
            # No reminder can reach an unstored lead, so none is promised.
            entry.update(createdAt=now.isoformat(), updatedAt=now.isoformat(), notifyWhenChanged=False, remindAt=None)
        else:
            attributes = dict(contact.attributes) if isinstance(contact.attributes, dict) else {}
            leads = dict(attributes.get(LEADS_KEY)) if isinstance(attributes.get(LEADS_KEY), dict) else {}
            previous = leads.get(key) if isinstance(leads.get(key), dict) else None
            if lead["reason"] in REASONS:
                # Plan E3: the exit's row, whichever reason it was first kept
                # under. A refined reason moves it; nothing else is lost.
                family = _exit_keys(leads, lead["domain"])
                rows = [leads[name] for name in family]
                if previous is None and rows:
                    # Rows kept before E3 may be several per domain: prefer
                    # the one the founder asked to be told about.
                    previous = next((row for row in rows if row.get("notifyWhenChanged") is True), rows[0])
                if previous is not None:
                    previous = dict(previous)
                    if any(row.get("notifyWhenChanged") is True for row in rows):
                        previous["notifyWhenChanged"] = True
                    reminded_rows = [row.get("remindedAt") for row in rows if row.get("remindedAt")]
                    if reminded_rows and not previous.get("remindedAt"):
                        previous["remindedAt"] = min(reminded_rows)
                for name in family:
                    leads.pop(name, None)
            created, stored = previous is None, True
            # Asking to be told is sticky: a later plain visit, or a later
            # notifyWhenChanged false, never withdraws it.
            notify = bool(lead["notify"] or (previous or {}).get("notifyWhenChanged") is True)
            previous = previous or {}
            reminded = previous.get("remindedAt")
            entry.update(createdAt=previous.get("createdAt") or now.isoformat(), updatedAt=now.isoformat(),
                         notifyWhenChanged=notify,
                         remindAt=((previous.get("remindAt") or (None if reminded else (now + REMIND_AFTER).isoformat()))
                                   if notify else None))
            # What the founder was shown, so a later change is news (E3b).
            state = guidance_state(entry)
            same_guidance = (previous.get("guidanceId"), previous.get("hostClass"), previous.get("reason")) == (
                entry["guidanceId"], entry["hostClass"], entry["reason"])
            entry["guidanceState"] = previous.get("guidanceState") if same_guidance and previous.get("guidanceState") else state
            # Reminder bookkeeping and an opt-out survive every refinement.
            for name in ("remindedAt", "reminder", "notices", "optedOutAt"):
                if name in previous:
                    entry[name] = previous[name]
            if lead["notify"] and entry.get("optedOutAt"):
                # Pressing "Tell me when this changes" again is a fresh opt-in.
                entry.pop("optedOutAt", None)
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
        if created and lead["reason"] in REASONS:
            from content_factory.founder_funnel import emit_event
            at, signed_up = now, getattr(user, "date_joined", None)
            transaction.on_commit(lambda: _emit(emit_event, user.pk, at, signed_up))
    if not stored:
        logger.info("founder_website_lead_not_stored user=%s reason=%s", user.pk, lead["reason"])
    public = {key: entry.get(key) for key in ("reason", "domain", "hostClass", "guidanceId", "notifyWhenChanged",
                                              "remindAt", "createdAt")}
    return public, created, stored


def record_staff_lead(organization, *, reason, cause, domain="", now=None):
    """Record a lead row when staff could not set up a founder's website (plan B25).

    The row goes on the startup owner's contact, beside the not-on-GitHub
    leads, so the same follow-up finds it when the cause is fixed. ``reason``
    is the lead kind; ``cause`` the closed-list "can't be done yet" reason.
    Returns (public lead, created, stored), or None without a billing user.
    """
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=getattr(organization, "billing_user_id", None)).first()
    if user is None:
        logger.info("founder_website_staff_lead_without_owner org=%s reason=%s", getattr(organization, "pk", None), reason)
        return None
    host_class = cause if HOST_CLASS.fullmatch(str(cause or "")) else "unknown"
    return record_lead(user, {"reason": reason, "domain": normalise_lead_domain(domain), "hostClass": host_class,
                              "notify": True}, now=now, source="staff_fixing.cant_be_done_yet")


def _emit(emit_event, user_id, at, signed_up):
    try:
        emit_event("not_on_github", user_id=user_id, at=at, signed_up_at=signed_up, source="website_lead.recorded")
    except Exception:  # noqa: BLE001 - telemetry never breaks the founder's action
        logger.exception("founder_website_lead_event_failed")


class FounderWebsiteLeadView(APIView):
    """Record the not-on-GitHub exit ("Tell me when this changes" sets notifyWhenChanged)."""

    def post(self, request):
        interest = isinstance(getattr(request, "data", None), dict) and request.data.get("reason") in INTEREST_REASONS
        if not interest and not lead_exit_enabled():
            return Response(dict(NOT_ENABLED), status=status.HTTP_404_NOT_FOUND)
        try:
            lead = parse_lead(request.data)
        except ValueError as exc:
            return Response({"detail": str(exc), "reason": "invalid_lead"}, status=status.HTTP_400_BAD_REQUEST)
        source = "my_startup.cms_content" if lead["reason"] in INTEREST_REASONS else "my_startup.website_step"
        public, created, stored = record_lead(request.user, lead, source=source)
        base = CMS_MESSAGE if lead["reason"] in INTEREST_REASONS else MESSAGE
        # "We'll email you" only for a request this server actually keeps.
        if stored and public["notifyWhenChanged"]:
            message = f"{base} {NOTIFY_MESSAGE}"
        elif not stored and lead["notify"]:
            message = f"{base} {NOT_STORED_MESSAGE}"
        else:
            message = base
        return Response({"lead": public, "recorded": stored, "message": message},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
