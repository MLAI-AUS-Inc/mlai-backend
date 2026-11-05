"""Stream identities and the protected root-domain boundary."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email, validate_slug
from django.db import transaction

from . import policy


def protected_domain(domain):
    """Subdomains may send campaigns; the exact protected domains may never do so."""
    return str(domain).strip().lower().rstrip(".") in {"mlai.au", *policy.value("CRM_PROTECTED_DOMAINS")}


def stream_definitions(raw=None):
    """Validate all configured identities before any stream-sync writes occur."""
    definitions = []
    keys, domains, purposes = set(), set(), set()
    for entry in (policy.value("CRM_STREAMS") if raw is None else raw).split(";"):
        if not entry.strip():
            continue
        parts = [part.strip().lower() for part in entry.split(":")]
        if len(parts) != 4:
            raise ValueError("CRM_STREAMS requires key:domain:address:purposes entries")
        key, domain, email, purpose_text = parts
        try:
            validate_slug(key)
            validate_email(email)
        except ValidationError as exc:
            raise ValueError("CRM_STREAMS contains an invalid identity") from exc
        choices = purpose_text.split("|")
        transactional = choices == ["transactional"]
        if (len(key) > 40 or domain != email.rsplit("@", 1)[-1]
                or key in keys or domain in domains
                or not set(choices) <= {"marketing", "event_update", "transactional"}
                or ("transactional" in choices and not transactional)
                or (not transactional and (protected_domain(domain) or purposes.intersection(choices)))):
            raise ValueError("CRM_STREAMS contains duplicate or unsafe identities")
        keys.add(key)
        domains.add(domain)
        if not transactional:
            purposes.update(choices)
        definitions.append({"key": key, "domain": domain, "email": email,
                            "purposes": [] if transactional else choices, "transactional": transactional})
    return definitions


def monitored_sender(sender):
    """Observe system mail on the root, every configured stream, and legacy senders."""
    sender = str(sender).lower()
    if "@" not in sender:
        return False
    domain = sender.rsplit("@", 1)[-1]
    return (protected_domain(domain)
            or domain in {item["domain"] for item in stream_definitions()}
            or sender in getattr(settings, "CRM_RESEND_MONITORED_SENDERS", ()))


def is_owner(user):
    """Owner capability supplements, and never replaces, the existing admin gate."""
    from community_chat.admin_access import is_mlai_admin_user
    return bool(is_mlai_admin_user(user) and str(user.email).lower() in policy.value("CRM_DELIVERABILITY_OWNERS"))


def stream_configured(stream):
    """A removed or mismatched stream cannot keep sending without feedback coverage."""
    return any(item["key"] == stream.key and item["domain"] == stream.domain
               and item["transactional"] == stream.transactional for item in stream_definitions())


def assign_stream(blast, *, actor=None, mailbox=None):
    """Choose the purpose's stream; stale clients cannot select a technical identity."""
    from crm.blasts import BlastError
    from crm.models import Channel, SendingStream
    if Channel.EMAIL not in (blast.channels or []):
        blast.stream = None
        return
    candidates = [stream for stream in SendingStream.objects.select_related("mailbox")
                  if blast.purpose in stream.purposes and stream_configured(stream)]
    if len(candidates) != 1:
        raise BlastError("stream_not_configured", 409)
    stream = candidates[0]
    if stream.transactional:
        raise BlastError("stream_not_for_blasts", 409)
    chosen = mailbox if mailbox is not None and is_owner(actor) else stream.mailbox
    if protected_domain(chosen.email.rsplit("@", 1)[-1]):
        raise BlastError("protected_domain", 409)
    if chosen.domain != stream.domain or chosen.email.rsplit("@", 1)[-1] != stream.domain:
        raise BlastError("mailbox_stream_mismatch", 409)
    blast.stream, blast.mailbox = stream, chosen


def sync_streams(*, actor=None):
    """Create disabled identities idempotently; never enable or reset stream state."""
    from crm.models import CrmAuditEvent, SenderMailbox, SendingStream
    definitions = stream_definitions()
    with transaction.atomic():
        for definition in definitions:
            mailbox, _ = SenderMailbox.objects.get_or_create(email=definition["email"], defaults={
                "domain": definition["domain"], "display_name": "MLAI", "reply_to": "hi@mlai.au",
                "route": "resend", "enabled": False,
            })
            if mailbox.domain != definition["domain"] or mailbox.route != "resend":
                raise ValueError("Existing mailbox route differs; owner review required")
            existing = SendingStream.objects.filter(key=definition["key"]).first()
            fields = {key: definition[key] for key in ("domain", "purposes", "transactional")}
            fields.update(mailbox=mailbox, label="System email" if definition["transactional"] else "Community email")
            if existing and any(getattr(existing, key) != val for key, val in fields.items()):
                # Identity changes require an owner review, not an automatic config overwrite.
                raise ValueError("Existing stream identity differs; owner review required")
            stream, created = SendingStream.objects.get_or_create(key=definition["key"], defaults=fields)
            if created:
                CrmAuditEvent.objects.create(actor=actor, action="edit", target_type="sending_stream",
                    target_id=str(stream.pk), detail={"operation": "create", "key": stream.key, "enabled": False})
    return len(definitions)
