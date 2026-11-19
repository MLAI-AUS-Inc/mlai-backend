"""Plan E3b: reminders for website leads, and notices when their guidance changes.

A lead (``website_leads``) has no organisation, so the turn reminders keyed on
startups never reach it. This sender is keyed on the lead row itself: the
user, the guidance id and ``remindAt``, kept in the CRM contact's
``attributes[LEADS_KEY]`` (no migration).

Two kinds of email, both only for a lead whose founder pressed "Tell me when
this changes" (``notifyWhenChanged``) and has not opted out:

* ``reminder``: once ``remindAt`` has passed, one email with a deep link back
  to the website step, prefilled. One per lead: after it ``remindAt`` is
  cleared and ``remindedAt`` kept, so no later sweep or refinement sends
  another.
* ``guidance_changed``: when the state of the guidance the founder saw
  (``guidanceState``, recorded with the lead) differs from today's
  (``guidance_state``), for example a builder's GitHub export was checked
  (A23) or decision D9 changed. One per lead and new state.

Guards:

* ``MY_STARTUP_WEBSITE_LEAD_REMINDERS_ENABLED`` (default off) gates the sweep.
  Off, nothing is read or sent.
* Each sweep sends at most ``SWEEP_CAP`` emails (``limit`` lowers it), so
  switching the flag on over an old backlog cannot burst (as A22 does for
  turn reminders). The rest wait for later sweeps.
* Deduplicated across redeploys: the send is claimed in the lead row under
  the contact's row lock before any provider call, with a stable idempotency
  key that ``system_email.send`` maps to one ``SystemEmail`` row and one
  provider idempotency key. A retryable failure is retried with the same key
  inside ``RETRY_WINDOW``; after it, or on a refusal, the claim is closed as
  failed and never resent.
* Opt-out: every email carries a signed one-click link
  (``FounderWebsiteLeadOptOutView``) that clears ``notifyWhenChanged`` on all of
  the contact's leads. The suppression list and a contact that unsubscribed,
  bounced or complained are honoured as well.
* Only a verified, active account whose email still matches the contact is
  emailed, and nobody who has since connected a website.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import logging
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

FLAG = "MY_STARTUP_WEBSITE_LEAD_REMINDERS_ENABLED"
MESSAGE_KEY = "founder_website_lead_reminder"
SWEEP_CAP = 10
RETRY_WINDOW = timedelta(hours=23)
OPT_OUT_SALT = "founder-website-lead-opt-out-v1"
OPT_OUT_PATH = "/api/v1/founder-tools/website-lead/opt-out/"

# What each guidance says today. Ids are Chat's builder guidance ids
# (``founderBuilderGuidance.ts`` and ``startup_builder_guidance.dart``).
# ``unverified`` is the interim generic copy; ``verified`` once A23 has a
# dated note for that builder (flip it here in the same change as Chat's
# entry). ``cant_connect_yet`` is decision D9 for builders with no export.
GUIDANCE_STATES = {
    "netlify": "unverified", "vercel": "unverified", "cloudflare_pages": "unverified", "render": "unverified",
    "github_pages": "unverified", "railway": "unverified",
    "lovable": "unverified", "bolt": "unverified", "replit": "unverified", "v0": "unverified",
    "emergent": "unverified", "base44": "unverified",
    "framer": "cant_connect_yet", "webflow": "cant_connect_yet", "wix": "cant_connect_yet",
    "squarespace": "cant_connect_yet", "shopify": "cant_connect_yet", "chatgpt_sites": "cant_connect_yet",
}
GUIDANCE_NAMES = {
    "netlify": "Netlify", "vercel": "Vercel", "cloudflare_pages": "Cloudflare Pages", "render": "Render",
    "github_pages": "GitHub Pages", "railway": "Railway", "lovable": "Lovable", "bolt": "Bolt",
    "replit": "Replit", "v0": "v0", "emergent": "Emergent", "base44": "Base44", "framer": "Framer",
    "webflow": "Webflow", "wix": "Wix", "squarespace": "Squarespace", "shopify": "Shopify",
    "chatgpt_sites": "ChatGPT sites",
}
# A lead without a known guidance id follows its reason, then its host class.
REASON_STATES = {"other_git_host": "move_to_github", "not_in_git": "move_to_github",
                 "someone_else_holds_code": "invite_developer", "no_github_account": "create_account",
                 "not_on_github": "generic"}
CLASS_STATES = {"no_export": "cant_connect_yet", "ai_builder": "unverified", "git_host": "unverified"}


def reminders_enabled() -> bool:
    return bool(getattr(settings, FLAG, False))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def guidance_state(lead) -> str:
    """``<subject>:<state>`` of the guidance this lead was shown; a change of it is news."""
    lead = _mapping(lead)
    guidance_id = str(lead.get("guidanceId") or "")
    if guidance_id in GUIDANCE_STATES:
        return f"{guidance_id}:{GUIDANCE_STATES[guidance_id]}"
    reason = str(lead.get("reason") or "")
    host_class = str(lead.get("hostClass") or "unknown")
    if host_class in CLASS_STATES:
        return f"{host_class}:{CLASS_STATES[host_class]}"
    if reason in REASON_STATES:
        return f"{reason}:{REASON_STATES[reason]}"
    return f"{reason or 'unknown'}:generic"


def _moment(value):
    if isinstance(value, datetime):
        return value
    parsed = parse_datetime(str(value or "")) if value else None
    return parsed if parsed is not None and not timezone.is_naive(parsed) else None


def _chat_origin():
    origin = str(getattr(settings, "COMMUNITY_CHAT_FRONTEND_URL", "") or "https://chat.mlai.au").rstrip("/")
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or parts.path not in {"", "/"}:
        return None
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1"}:
        return None
    return origin


def deep_link(lead) -> str | None:
    """The website step (stage 1), prefilled with the lead's address and builder."""
    origin = _chat_origin()
    if origin is None:
        return None
    lead = _mapping(lead)
    query = {"new": "true", "step": "website"}
    if lead.get("domain"):
        query["website"] = str(lead["domain"])
    if lead.get("guidanceId"):
        query["builder"] = str(lead["guidanceId"])
    return f"{origin}/my-startup/start?{urlencode(query)}"


def _email_digest(email) -> str:
    return hashlib.sha256(str(email or "").encode()).hexdigest()[:24]


def opt_out_token(contact) -> str:
    """Signed, not encrypted: it carries a digest of the address, never the address itself."""
    return signing.dumps({"c": str(contact.pk), "h": _email_digest(contact.email)}, salt=OPT_OUT_SALT)


def opt_out_url(contact) -> str:
    base = str(getattr(settings, "DEFAULT_BACKEND_URL", "") or "").rstrip("/")
    return f"{base}{OPT_OUT_PATH}?{urlencode({'token': opt_out_token(contact)})}"


def opt_out(token, *, now=None) -> str:
    """Clear "Tell me when this changes" on every lead of the token's contact.

    Returns ``opted_out``, ``already`` or ``invalid``. Nothing else on the
    contact changes.
    """
    from crm.models import Contact
    from .website_leads import LEADS_KEY
    try:
        claim = signing.loads(str(token or ""), salt=OPT_OUT_SALT)
    except signing.BadSignature:
        return "invalid"
    if not isinstance(claim, dict):
        return "invalid"
    now = now or timezone.now()
    with transaction.atomic():
        contact = Contact.objects.select_for_update().filter(pk=claim.get("c")).first()
        if contact is None or claim.get("h") != _email_digest(contact.email):
            return "invalid"
        attributes = deepcopy(_mapping(contact.attributes))
        leads = _mapping(attributes.get(LEADS_KEY))
        changed = False
        for lead in leads.values():
            if isinstance(lead, dict) and (lead.get("notifyWhenChanged") or lead.get("remindAt")):
                lead.update(notifyWhenChanged=False, remindAt=None, optedOutAt=now.isoformat())
                changed = True
        if not changed:
            return "already"
        attributes[LEADS_KEY] = leads
        Contact.objects.filter(pk=contact.pk).update(attributes=attributes, updated_at=now)
    logger.info("founder_website_lead_opted_out contact=%s", contact.pk)
    return "opted_out"


def _wants_email(lead) -> bool:
    lead = _mapping(lead)
    return lead.get("version") == 1 and lead.get("notifyWhenChanged") is True and not lead.get("optedOutAt")


def _is_exit(lead) -> bool:
    from .website_leads import REASONS
    return _mapping(lead).get("reason") in REASONS


def _due(lead, *, now):
    """What this lead is owed now: (kind, state) or None."""
    lead = _mapping(lead)
    if not _wants_email(lead):
        return None
    current = guidance_state(lead)
    seen = lead.get("guidanceState")
    if seen and seen != current and current not in _mapping(lead.get("notices")):
        return "guidance_changed", current
    remind_at = _moment(lead.get("remindAt"))
    # The "is it on GitHub yet?" reminder belongs to the not-on-GitHub exit
    # only; a CMS or staff "can't be done yet" lead hears about changes only.
    if remind_at is not None and remind_at <= now and not lead.get("remindedAt") and _is_exit(lead):
        return "reminder", current
    return None


def _send_key(contact, lead_key, kind, state, lead):
    stamp = lead.get("createdAt") if kind == "reminder" else state
    digest = hashlib.sha256(f"{contact.pk}|{lead_key}|{kind}|{stamp}".encode()).hexdigest()[:40]
    return f"founder-website-lead/{kind}/{digest}"


def _recipient(contact, lead):
    """The account to email, or None (and why)."""
    from django.contrib.auth import get_user_model
    from crm.emails import normalize_contact_email
    from crm.models import Contact
    if contact.email_status != Contact.EmailStatus.ACTIVE:
        return None, "contact_not_active"
    user = get_user_model().objects.filter(pk=lead.get("userId"), is_active=True).first()
    if user is None or contact.user_id not in (None, user.pk):
        return None, "no_account"
    if not getattr(user, "email_verified_at", None) or normalize_contact_email(user.email) != contact.email:
        return None, "email_changed"
    from content_factory.website_models import WebsiteConnection
    if WebsiteConnection.objects.filter(organization__billing_user_id=user.pk,
                                        state=WebsiteConnection.State.CONNECTED).exists():
        return None, "already_connected"
    return user, None


def _copy(kind, lead, link, unsubscribe):
    from system_email.render import RenderedEmail
    domain = lead.get("domain") or "your website"
    name = GUIDANCE_NAMES.get(str(lead.get("guidanceId") or ""))
    if kind == "reminder":
        subject = "Is your website's code on GitHub yet?"
        opening = (f"A week ago you asked us to remind you about {domain}. MLAI adds articles to websites whose "
                   "code is on GitHub. Once yours is, pick up where you left off: your website is filled in for you.")
    else:
        subject = "There's news about connecting your website to MLAI"
        what = f"websites built with {name}" if name else "websites like yours"
        opening = (f"You asked us to tell you when anything changed for {what} ({domain}). It has: the steps "
                   "to connect it are new. Have a look, your website is filled in for you.")
    text = (f"{opening}\n\nContinue: {link}\n\n"
            "You're getting this because you pressed \"Tell me when this changes\" in MLAI Chat. "
            f"Stop these emails: {unsubscribe}\n")
    return RenderedEmail(subject=subject, html="", text=text)


def _claim(contact_pk, lead_key, *, now):
    """Under the contact's lock: reserve this lead's next send, or None when nothing is owed."""
    from crm.models import Contact
    from .website_leads import LEADS_KEY
    contact = Contact.objects.select_for_update().filter(pk=contact_pk).first()
    if contact is None:
        return None
    attributes = deepcopy(_mapping(contact.attributes))
    leads = _mapping(attributes.get(LEADS_KEY))
    lead = leads.get(lead_key)
    if not isinstance(lead, dict):
        return None
    pending = _mapping(lead.get("reminder"))
    if pending.get("status") == "sending":
        started = _moment(pending.get("at"))
        wanted = _wants_email(lead)
        if wanted and started is not None and now - started < RETRY_WINDOW:
            return contact, lead, dict(pending)
        # An opt-out after the claim wins over the retry.
        pending = {**pending, "status": "failed" if wanted else "skipped",
                   "error": "retry_window_closed" if wanted else "opted_out"}
        lead["reminder"] = pending
        attributes[LEADS_KEY] = leads
        Contact.objects.filter(pk=contact.pk).update(attributes=attributes)
        return None
    due = _due(lead, now=now)
    if due is None:
        return None
    kind, state = due
    claim = {"kind": kind, "state": state, "key": _send_key(contact, lead_key, kind, state, lead),
             "status": "sending", "at": now.isoformat()}
    lead["reminder"] = claim
    if kind == "reminder":
        lead.update(remindedAt=now.isoformat(), remindAt=None)
    else:
        # The state the founder already saw is never news again (a revert is quiet).
        seen = {str(lead.get("guidanceState")): now.isoformat()} if lead.get("guidanceState") else {}
        lead["notices"] = {**seen, **_mapping(lead.get("notices")), state: now.isoformat()}
        lead["guidanceState"] = state
    attributes[LEADS_KEY] = leads
    Contact.objects.filter(pk=contact.pk).update(attributes=attributes)
    contact.attributes = attributes
    return contact, lead, claim


def _close(contact_pk, lead_key, claim, status, error=""):
    from crm.models import Contact
    from .website_leads import LEADS_KEY
    with transaction.atomic():
        contact = Contact.objects.select_for_update().filter(pk=contact_pk).first()
        if contact is None:
            return
        attributes = deepcopy(_mapping(contact.attributes))
        lead = _mapping(_mapping(attributes.get(LEADS_KEY)).get(lead_key))
        if _mapping(lead.get("reminder")).get("key") != claim["key"]:
            return
        lead["reminder"] = {**claim, "status": status, **({"error": error} if error else {})}
        Contact.objects.filter(pk=contact.pk).update(attributes=attributes)


def _deliver(contact, lead_key, lead, claim, *, send):
    from system_email.api import SystemEmailRejected
    from system_email.transport_resend import SystemEmailRetryable
    user, why = _recipient(contact, lead)
    link = deep_link(lead)
    if user is None or link is None:
        _close(contact.pk, lead_key, claim, "skipped", why or "no_link")
        return "skipped"
    rendered = _copy(claim["kind"], lead, link, opt_out_url(contact))
    try:
        send(MESSAGE_KEY, to=user.email, idempotency_key=claim["key"], prerendered=rendered, user_id=user.pk,
             unsubscribe_url=opt_out_url(contact))
    except SystemEmailRetryable as exc:
        logger.warning("founder_website_lead_reminder_retry contact=%s code=%s", contact.pk, getattr(exc, "code", ""))
        return "retry"
    except SystemEmailRejected as exc:
        _close(contact.pk, lead_key, claim, "failed", str(getattr(exc, "code", "rejected"))[:80])
        return "failed"
    _close(contact.pk, lead_key, claim, "sent")
    return "sent"


def run_lead_reminder_sweep(now=None, limit=None, *, send=None):
    """Send what leads are owed, at most ``SWEEP_CAP`` (or ``limit``) emails. Flag-gated; never raises per lead."""
    if not reminders_enabled():
        return {"skipped": "flag_off"}
    from crm.models import Contact
    from .website_leads import LEADS_KEY
    if send is None:
        from system_email.api import send
    now = now or timezone.now()
    cap = SWEEP_CAP if limit is None else max(0, min(int(limit), SWEEP_CAP))
    counts = {"sent": 0, "skipped": 0, "failed": 0, "retry": 0, "deferred": 0}
    contacts = Contact.objects.filter(attributes__has_key=LEADS_KEY).order_by("pk").values_list("pk", "attributes")
    for contact_pk, attributes in contacts.iterator(chunk_size=200):
        for lead_key, lead in sorted(_mapping(_mapping(attributes).get(LEADS_KEY)).items()):
            pending = _mapping(_mapping(lead).get("reminder")).get("status") == "sending"
            if not pending and _due(lead, now=now) is None:
                continue
            if counts["sent"] + counts["retry"] >= cap:
                counts["deferred"] += 1
                continue
            try:
                with transaction.atomic():
                    claimed = _claim(contact_pk, lead_key, now=now)
                if claimed is None:
                    continue
                contact, claimed_lead, claim = claimed
                outcome = _deliver(contact, lead_key, claimed_lead, claim, send=send)
            except Exception:  # noqa: BLE001 - one lead never stops the sweep
                logger.exception("founder_website_lead_reminder_failed contact=%s", contact_pk)
                counts["failed"] += 1
                continue
            counts[outcome] += 1
    return counts


PAGE = ('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>MLAI email preferences</title><style>body{{font:1rem system-ui;max-width:32rem;margin:4rem auto;'
        'padding:1.5rem;line-height:1.5}}button{{font:inherit;padding:.75rem 1rem;border-radius:.5rem;'
        'background:#222;color:#fff;border:0;cursor:pointer}}</style></head><body><h1>MLAI website reminders</h1>'
        '{}</body></html>')


@csrf_exempt
@require_http_methods(["GET", "POST"])
def opt_out_view(request):
    """The email's opt-out link. GET only asks (link scanners change nothing); POST, or one-click, opts out.

    The signed token is the whole authority: it names one CRM contact, and the
    only thing it can do is stop these reminders for that contact.
    """
    token = request.GET.get("token", "") if request.method == "GET" else (
        request.POST.get("token") or request.GET.get("token", ""))
    if request.method == "GET":
        try:
            signing.loads(str(token or ""), salt=OPT_OUT_SALT)
            valid = True
        except signing.BadSignature:
            valid = False
        content = format_html(
            '<p>Stop emails about connecting your website to MLAI?</p><form method="post" action="{}">'
            '<input type="hidden" name="token" value="{}"><button type="submit">Stop these emails</button></form>',
            OPT_OUT_PATH, token) if valid else format_html("<p>{}</p>", INVALID)
        status = 200 if valid else 400
    else:
        result = opt_out(token)
        content = format_html("<p>{}</p>", INVALID if result == "invalid" else DONE)
        status = 400 if result == "invalid" else 200
    response = HttpResponse(format_html(PAGE, content), status=status)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                           "base-uri 'none'; frame-ancestors 'none'")
    return response


DONE = "Done. We won't email you about connecting your website any more."
INVALID = "This link is no longer available. Nothing has been changed."
