"""Recipient-bound Startup Team invitations and their lifecycle."""
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.email_aliases import lock_email_namespace
from core.models import User
from system_email.models import SystemEmail

from . import membership, startup_invite_email, startup_team as team
from .models import StartupMembership, StartupTeamInvitation as Invitation, StartupTeamInviteDelivery as Delivery

LIFETIME = timedelta(days=7)
COOLDOWN = timedelta(minutes=5)
MAX_PENDING = 50


def sending_enabled():
    """Pause new mail independently of the durable link retirement marker."""
    from system_email import transport_resend

    return bool(settings.MY_STARTUP_EMAIL_INVITES_ENABLED and startup_invite_email.frontend_origin()
                and transport_resend.api_key() and transport_resend.from_address())


def delivery_key(delivery_id):
    """Each user-initiated send has its own durable provider idempotency key."""
    return f"startup-team-invite/{delivery_id}"


def _unavailable():
    return team.TeamError("invitation_unavailable", "This invitation is unavailable.", 404)


def _open(rows):
    return rows.filter(revoked_at__isnull=True, accepted_at__isnull=True)


def _email(value):
    address = User.objects.normalize_email(value) if isinstance(value, str) else ""
    try:
        if len(address) > 254 or address.endswith("@slack.placeholder.com"):
            raise ValidationError("Address unavailable.")
        validate_email(address)
    except ValidationError:
        raise team.TeamError("invalid_email", "Enter a valid email address.", 400) from None
    return address


def _verified_addresses(user):
    addresses = set(user.email_aliases.filter(verified_at__isnull=False).values_list("email", flat=True))
    if user.email_verified_at:
        addresses.add(User.objects.normalize_email(user.email))
    return addresses


def _active_member(company, user, roster):
    if user is None or not user.is_active:
        return False
    if company.membership_policy == company.SHARED_ACCESS:
        return membership.has_membership(user, company)
    return bool(roster and (user.pk == roster.get("owner_user_id") or team._member(roster, user)))


def _check_workspace(company, roster):
    canonical = membership.shared_workspace(company.organization)
    if (not company.email_invites_enabled or company.closed_at
            or (roster or {}).get("frozen")
            or (canonical and canonical.pk != company.pk)
            or (not canonical and (not team.enabled() or team.mapping((roster or {}).get("company")).get("id") != str(company.pk)))):
        raise _unavailable()


def _reserve_delivery(company, actor, email, now):
    recent = Delivery.objects.filter(created_at__gt=now - timedelta(days=1))
    rules = (
        (recent.filter(actor=actor), 20),
        (recent.filter(invitation__company=company), 20),
        (recent.filter(invitation__email=email), 5),
    )
    for rows, limit in rules:
        recent_times = list(rows.order_by("-created_at").values_list("created_at", flat=True)[:limit])
        if len(recent_times) == limit:
            retry = recent_times[-1] + timedelta(days=1)
            raise team.TeamError("invite_rate_limited", "Try again later.", 429, retry_at=retry)


def _summary(invitation, delivery, log, now):
    status = delivery.state if delivery else Delivery.State.FAILED
    if log and log.status in {"bounced", "complained", "suppressed", "failed"}:
        status = "failed"
    elif log and log.status == "delivered":
        status = "delivered"
    elif log and log.status in {"sent", "delayed"}:
        status = log.status
    elif status == Delivery.State.SENDING:
        status = "queued"
    can_resend = bool(delivery and sending_enabled() and delivery.created_at + COOLDOWN <= now
                      and status not in {"queued", "cancelled"}
                      and (status != "failed" or (delivery.retryable and not (log and log.status in {
                          "bounced", "complained", "suppressed",
                      }))))
    return {
        "invitationId": str(invitation.pk), "email": invitation.email, "deliveryStatus": status,
        "expiresAt": invitation.expires_at.isoformat(), "canResend": can_resend,
        "canCancel": True,
        "resendAfter": (delivery.created_at + COOLDOWN).isoformat() if delivery else None,
    }


def pending(company):
    """Return only the management representation for this selected company."""
    now = timezone.now()
    rows = list(_open(Invitation.objects.filter(company=company, expires_at__gt=now))
                .order_by("-issued_at")[:MAX_PENDING])
    deliveries = {item.public_id: item for item in Delivery.objects.filter(
        invitation__in=rows, public_id__in=[row.public_id for row in rows],
    )}
    logs = {item.idempotency_key: item for item in SystemEmail.objects.filter(
        idempotency_key__in=[delivery_key(item.pk) for item in deliveries.values()],
    )}
    return [_summary(row, deliveries.get(row.public_id),
                     logs.get(delivery_key(deliveries[row.public_id].pk)) if row.public_id in deliveries else None,
                     now) for row in rows]


@transaction.atomic
def send_invitation(company, actor, *, email=None, invitation_id=None, action_id=None):
    """Issue one invite or rotate it for an explicit, replayable resend."""
    if invitation_id is not None:
        try:
            action_id = uuid5(invitation_id, str(UUID(str(action_id))))
        except (ValueError, TypeError):
            raise team.TeamError("invalid_action", "Try sending again.", 400) from None
        email = Invitation.objects.filter(company=company, pk=invitation_id).values_list("email", flat=True).first()
        if not email:
            raise _unavailable()
    email = _email(email)
    # Take per-recipient and per-actor locks before the organization lock so
    # sends from different startups reserve the same limits consistently.
    lock_email_namespace(email)
    lock_email_namespace(f"startup-invites:actor:{actor.pk}")
    with team._locked(company.organization, create=True) as (_, roster):
        previous_organization = company.organization_id
        company.refresh_from_db()
        if company.organization_id != previous_organization:
            raise _unavailable()
        _check_workspace(company, roster)
        team._manage(actor, company.organization, roster)
        now = timezone.now()
        rows = _open(Invitation.objects.filter(company=company))
        current = rows.filter(email=email).first()
        if invitation_id is not None and Delivery.objects.filter(invitation_id=invitation_id, pk=action_id).exists():
            return {"status": "existing"}
        if not sending_enabled():
            raise team.TeamError("invite_sending_paused", "Invitations are temporarily unavailable.", 503)
        recipient = User.objects.resolve_email(email)
        if _active_member(company, recipient, roster):
            return {"status": "already_member"}
        if invitation_id is not None:
            if not current or current.pk != invitation_id or current.expires_at <= now:
                raise _unavailable()
            delivery = current.deliveries.get(public_id=current.public_id)
            if delivery.created_at + COOLDOWN > now:
                raise team.TeamError("invite_rate_limited", "Try again later.", 429,
                                     retry_at=delivery.created_at + COOLDOWN)
            log = SystemEmail.objects.filter(idempotency_key=delivery_key(delivery.pk)).first()
            if not _summary(current, delivery, log, now)["canResend"]:
                raise team.TeamError("invite_resend_unavailable", "This invitation cannot be resent yet.", 409)
        elif current and current.expires_at > now:
            return {"status": "existing"}
        else:
            if rows.filter(expires_at__gt=now).count() >= MAX_PENDING:
                raise team.TeamError("invite_limit", "Cancel a pending invitation before sending another.", 409)
            if current:
                rows.filter(pk=current.pk).update(revoked_at=now)
            current = Invitation(company=company, email=email)
        _reserve_delivery(company, actor, email, now)
        current.public_id = uuid4()
        current.inviter = actor
        current.policy = company.membership_policy
        current.issued_at = now
        current.expires_at = now + LIFETIME
        current.save()
        delivery = Delivery.objects.create(
            id=action_id or uuid4(), invitation=current, actor=actor, public_id=current.public_id,
            context=startup_invite_email.template_context(current), created_at=now, available_at=now,
        )
        return {"status": "queued", "invitation": _summary(current, delivery, None, now)}


def cancel(company, actor, invitation_id):
    """Revoke a pending invitation in the same scope as issuance."""
    with team._locked(company.organization) as (_, roster):
        previous_organization = company.organization_id
        company.refresh_from_db()
        if company.organization_id != previous_organization:
            raise _unavailable()
        _check_workspace(company, roster)
        team._manage(actor, company.organization, roster)
        row = Invitation.objects.filter(company=company, pk=invitation_id, accepted_at__isnull=True).first()
        if not row:
            raise _unavailable()
        if row.revoked_at is None:
            row.revoked_at = timezone.now()
            row.save(update_fields=["revoked_at"])
        return {"status": "cancelled"}


def revoke_open(organization, now, *, person=None):
    """Fence pending invitations on removal, offboarding and policy changes."""
    rows = _open(Invitation.objects.filter(company__organization=organization))
    if person is not None:
        rows = rows.filter(Q(inviter=person) | Q(email__in=_verified_addresses(person)))
    rows.update(revoked_at=now)


def _eligible(invitation, company, roster, now):
    _check_workspace(company, roster)
    if (invitation.revoked_at or invitation.expires_at <= now
            or invitation.policy != company.membership_policy
            or not _active_member(company, invitation.inviter, roster)):
        raise _unavailable()
    if company.membership_policy != company.SHARED_ACCESS:
        if team._owner(company.organization) != invitation.inviter_id:
            raise _unavailable()
    elif StartupMembership.objects.filter(
        company=company, user_id=invitation.inviter_id, joined_at__gt=invitation.issued_at,
    ).exists():
        raise _unavailable()


def accept(user, public_id, *, confirm=False):
    """Preview or accept using a live, verified identity. A GET never joins."""
    if not team._valid_user(user):
        raise _unavailable()
    found = Invitation.objects.select_related("company__organization").filter(public_id=public_id).first()
    if found is None or found.company.organization_id is None:
        raise _unavailable()
    with team._locked(found.company.organization) as (config, roster):
        invitation = Invitation.objects.select_related("company__organization", "inviter").filter(
            pk=found.pk, public_id=public_id,
        ).first()
        user = User.objects.filter(pk=user.pk, is_active=True).first()
        if not invitation or not user or invitation.email not in _verified_addresses(user):
            raise _unavailable()
        company = invitation.company
        if company.organization_id != found.company.organization_id:
            raise _unavailable()
        now = timezone.now()
        if invitation.accepted_at:
            if invitation.accepted_by_id == user.pk and not company.closed_at and _active_member(company, user, roster):
                return {"status": "joined", "companyId": str(company.pk)}
            raise _unavailable()
        _eligible(invitation, company, roster, now)
        shared = company.membership_policy == company.SHARED_ACCESS
        if not confirm:
            return {"status": "pending", "startupName": company.name,
                    "inviterName": invitation.inviter.full_name or "Your teammate",
                    "accessPolicy": "shared" if shared else "legacy"}
        if shared:
            if StartupMembership.objects.filter(company=company, user=user, removed_at__gte=invitation.issued_at).exists():
                raise _unavailable()
            _, joined = team._admit_shared(company, user, source="email_invite", actor=invitation.inviter, now=now)
        else:
            member = team._member(roster, user)
            if member and member["state"] == "unconfirmed":
                member.update(state="active", via="email_invite", approved_by_user_id=invitation.inviter_id)
                team._history(roster, "joined", invitation.inviter, user, now)
                joined = True
            else:
                _, joined = team._join(roster, user, state="active", via="email_invite", actor=invitation.inviter, now=now)
        if roster:
            for request in roster["requests"]:
                if request.get("user_id") == user.pk and request.get("state") == "pending":
                    request.update(state="approved", decided_at=now.isoformat(), decided_by_user_id=invitation.inviter_id)
            team._save(config, roster)
        invitation.accepted_at = now
        invitation.accepted_by = user
        invitation.save(update_fields=["accepted_at", "accepted_by"])
        if joined:
            team._notify("notify_member_joined", company.organization, user)
        return {"status": "joined", "companyId": str(company.pk)}
