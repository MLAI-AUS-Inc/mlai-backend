"""Canonical recipients and atomic admission for manual Roo adjustments."""

import hashlib
import json
import re
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from core.models import User

from .models import Ledger, PointsAdmin
from .permissions import IdempotencyConflictError, PermissionDeniedError, is_points_admin
from .services import PointsService


class ManualAwardError(ValueError):
    """A validated manual adjustment failure with an HTTP response status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def parse_manual_award(data):
    """Validate the recipient union without coercing names into Slack IDs."""
    values = {}
    for field in ("target_profile_id", "target_slack_id", "idempotency_key"):
        value = data.get(field)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ManualAwardError(f"{field} must be a string")
        values[field] = value.strip()
    if bool(values["target_profile_id"]) == bool(values["target_slack_id"]):
        raise ManualAwardError("Provide exactly one of target_profile_id or target_slack_id")
    if values["target_profile_id"]:
        try:
            values["target_profile_id"] = str(uuid.UUID(values["target_profile_id"]))
        except ValueError as exc:
            raise ManualAwardError("target_profile_id must be a profile UUID") from exc
        if not values["idempotency_key"]:
            raise ManualAwardError("idempotency_key is required for a profile award")
    elif not re.fullmatch(r"[UW][A-Z0-9]+", values["target_slack_id"]):
        raise ManualAwardError("target_slack_id must be a Slack user ID")
    if len(values["idempotency_key"]) > 200:
        raise ManualAwardError("idempotency_key must be at most 200 characters")
    points = data.get("points")
    if isinstance(points, bool) or not (
        isinstance(points, int) or isinstance(points, str) and re.fullmatch(r"[+-]?\d+", points)
    ):
        raise ManualAwardError("Points must be an integer")
    values["points"] = int(points)
    if not values["points"] or abs(values["points"]) > 2_147_483_647:
        raise ManualAwardError("Points must be a nonzero 32-bit integer")
    reason = data.get("reason", "Manual adjustment")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ManualAwardError("reason must contain 1 to 2000 characters")
    values["reason"] = reason.strip()
    return values


def _operation_key(actor, operation):
    community = (
        getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_COMMUNITY", "")
        or urlparse(getattr(settings, "COMMUNITY_CHAT_RELAY_URL", "")).hostname
        or "mlai"
    )
    raw = json.dumps([community, actor.pk, operation], separators=(",", ":"))
    return "manual:v2:" + hashlib.sha256(raw.encode()).hexdigest()


@transaction.atomic
def adjust_points(*, actor, admin_slack_id, values, resolve_slack_user):
    """Adjust one existing account once, serializing each admin's allowance.

    The caller authenticates the Roo service and resolves the linked human
    actor. Recipient profile IDs never invoke Slack lookup or user creation.
    """
    if values["target_profile_id"]:
        recipient = User.objects.filter(
            community_chat_profile_id=values["target_profile_id"],
        ).first()
    else:
        recipient = resolve_slack_user(values["target_slack_id"])
    if recipient is None:
        raise ManualAwardError("Recipient is not an active Chat member", 404)

    # Lock canonical accounts in one order before lower-level role/wallet
    # rows. Cross-awards by two administrators cannot deadlock on each other.
    accounts = {user.pk: user for user in User.objects.select_for_update().filter(
        pk__in={actor.pk, recipient.pk},
    ).order_by("pk")}
    actor, recipient = accounts.get(actor.pk), accounts.get(recipient.pk)
    linked_actor = PointsService.get_user_by_slack_id(admin_slack_id)
    if (actor is None or not actor.is_active or linked_actor is None
            or linked_actor.pk != actor.pk):
        raise PermissionDeniedError("A currently linked active account is required to award points manually")
    if recipient is None or not recipient.is_active:
        raise ManualAwardError("Recipient is not active", 404)
    if values["target_slack_id"] and recipient.slack_id != values["target_slack_id"]:
        raise ManualAwardError("Recipient Slack identity changed; select the member again", 409)
    admin = PointsAdmin.objects.select_for_update().filter(slack_user_id=admin_slack_id).first()
    if not is_points_admin(admin_slack_id):
        raise PermissionDeniedError("Only Points Admins can award points manually")
    if admin is not None and admin.user_id not in (None, actor.pk):
        raise PermissionDeniedError("Points Admin identity no longer matches its linked account")
    if values["target_profile_id"] and not CommunityChatDevice.objects.select_for_update().filter(
        user=recipient, status=DeviceBindingStatus.VERIFIED, revoked_at__isnull=True,
    ).first():
        raise ManualAwardError("Recipient is not an active Chat member", 404)
    points, reason = values["points"], values["reason"]
    if points > 0 and recipient.pk == actor.pk:
        raise ManualAwardError("Admins cannot award points to themselves")

    operation = values["idempotency_key"] or f"legacy:{uuid.uuid4()}"
    key = _operation_key(actor, operation)
    fingerprint = hashlib.sha256(json.dumps(
        [recipient.pk, points, reason], separators=(",", ":"),
    ).encode()).hexdigest()
    existing = Ledger.objects.filter(idempotency_key=key).first()
    if existing is not None:
        if (existing.user_id != recipient.pk or existing.delta != points
                or existing.source != "MANUAL" or existing.description != reason
                or existing.created_by_slack_id != admin_slack_id
                or existing.reference_type != "MANUAL_ADJUSTMENT"
                or existing.reference_id != fingerprint):
            raise IdempotencyConflictError("Award operation was already used with different details")
        ledger, created = existing, False
    else:
        if points > 0 and admin is not None:
            allowance = PointsService.get_admin_allowance_status(admin_slack_id)
            if allowance.get("error") or points > allowance.get("remaining", 0):
                raise ManualAwardError("Award exceeds the administrator's remaining weekly allowance")
        method = PointsService.award if points > 0 else PointsService.spend
        ledger, created = method(
            user=recipient, delta=abs(points), source="MANUAL", description=reason,
            created_by_slack_id=admin_slack_id, idempotency_key=key,
            reference_type="MANUAL_ADJUSTMENT", reference_id=fingerprint,
        )
    return {
        "success": True,
        "new_balance": PointsService.get_balance(recipient)["balance"],
        "ledger_id": ledger.pk,
        "created": created,
        "replayed": not created,
        "recipient": {
            "kind": "mlai_profile",
            "profile_id": str(recipient.community_chat_profile_id),
            "display_name": recipient.full_name or "MLAI member",
            "slack_user_id": recipient.slack_id or None,
        },
        "message": f"{'Awarded' if points > 0 else 'Deducted'} {abs(points)} points",
    }
