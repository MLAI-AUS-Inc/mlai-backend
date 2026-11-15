"""Admin → Users and teams: who holds MLAI admin access, and changes to it.

Admin access is ``roo.permissions.mlai_admin_role``: a superuser, or an active
``roo.PointsAdmin`` row linked to the account with role ``admin`` or
``committee``. The same rows carry Roo's points-admin roles in Slack
(``portfolio_lead`` and ``partner`` too) and the committee team ("portfolio").

Rules:

* Any MLAI admin may look. Only a superuser or an ``admin`` may change anyone.
* Superusers are managed in Django and are read-only here.
* Nobody changes their own role or removes themselves, so nobody locks
  themselves out by accident. Changing your own team is fine.
* The last ``admin`` row can't be demoted or removed while no active
  superuser exists.
* Removing someone deactivates their row (never deletes it), which also ends
  their Roo points-admin role and keeps their ledger history.

Every change is written to ``crm.CrmAuditEvent`` as an ``edit`` with
``target_type="admin_team_member"``.
"""

from __future__ import annotations

import logging
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from roo.models import PointsAdmin
from roo.permissions import MLAI_ADMIN_ROLES, mlai_admin_role

logger = logging.getLogger(__name__)

AUDIT_TARGET = "admin_team_member"
#: Roles this screen can grant. Other Roo roles show but are set in Django.
GRANTABLE_ROLES = ("admin", "committee")
ROLE_LABELS = dict(PointsAdmin.ROLE_CHOICES) | {"superuser": "Superuser"}
TEAM_LABELS = dict(PointsAdmin.PORTFOLIO_CHOICES)
ROLE_ORDER = ("superuser", "admin", "committee", "portfolio_lead", "partner")
MANAGER_ROLES = ("superuser", "admin")
SEARCH_MIN = 2
SEARCH_MAX = 100
SEARCH_LIMIT = 10
#: Placeholder Slack ID for an account that has never linked Slack. The
#: column is unique and required; this can never match a real Slack ID.
ACCOUNT_SLACK_PREFIX = "account:"


class TeamError(Exception):
    """A refused change, answered as ``{"error": code}`` with ``status``."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def can_manage(user) -> bool:
    """Superusers and ``admin`` rows may change the team; committee may look."""

    return mlai_admin_role(user) in MANAGER_ROLES


def _require_manager(actor) -> None:
    if not can_manage(actor):
        raise TeamError("not_allowed", 403)


def _clean_team(team):
    if team in (None, ""):
        return None
    if team not in TEAM_LABELS:
        raise TeamError("invalid_team")
    return team


def _clean_role(role):
    if role not in GRANTABLE_ROLES:
        raise TeamError("invalid_role")
    return role


def _account(user) -> dict:
    return {
        "account_id": str(user.community_chat_profile_id),
        "name": user.full_name or user.email,
        "email": user.email,
        "avatar_url": user.avatar_url or None,
    }


def _member(row: PointsAdmin | None, user, actor) -> dict:
    """One person on the screen: a ``PointsAdmin`` row, a superuser, or both."""

    superuser = bool(user and user.is_active and user.is_superuser)
    role = "superuser" if superuser else row.role
    linked = user is not None
    admin_access = bool(
        linked and user.is_active and (superuser or (row is not None and row.role in MLAI_ADMIN_ROLES))
    )
    person = _account(user) if linked else {
        "account_id": None,
        "name": row.slack_user_id,
        "email": None,
        "avatar_url": None,
    }
    is_self = bool(linked and actor is not None and user.pk == actor.pk)
    manageable = row is not None and not superuser
    return {
        "id": row.pk if row is not None else None,
        **person,
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        # A superuser's own Roo role, if they also have a row.
        "points_role": row.role if row is not None else None,
        "team": row.portfolio if row is not None else None,
        "admin_access": admin_access,
        "linked": linked,
        "is_self": is_self,
        "can_change_role": manageable and not is_self,
        "can_change_team": manageable,
        "can_remove": manageable and not is_self,
        "added_at": row.created_at.isoformat() if row is not None and row.created_at else None,
    }


def _sort_key(member: dict):
    role = member["role"]
    rank = ROLE_ORDER.index(role) if role in ROLE_ORDER else len(ROLE_ORDER)
    return (not member["admin_access"], rank, (member["name"] or "").lower())


def list_team(actor) -> dict:
    """Everyone with admin access or a Roo team role, plus the choices to offer."""

    User = get_user_model()
    rows = list(PointsAdmin.objects.filter(is_active=True).select_related("user"))
    members = []
    seen_users = set()
    for row in rows:
        user = row.user
        if user is not None:
            if user.pk in seen_users:
                continue
            seen_users.add(user.pk)
        members.append(_member(row, user, actor))
    for user in User.objects.filter(is_superuser=True, is_active=True).exclude(pk__in=seen_users):
        members.append(_member(None, user, actor))
    members.sort(key=_sort_key)
    return {
        "can_manage": can_manage(actor),
        "members": members,
        "roles": [{"key": key, "label": ROLE_LABELS[key]} for key in GRANTABLE_ROLES],
        "teams": [{"key": key, "label": label} for key, label in PointsAdmin.PORTFOLIO_CHOICES],
        "admin_count": sum(1 for member in members if member["admin_access"]),
    }


def search_accounts(actor, query: str) -> list[dict]:
    """Up to ten active accounts matching a name or email, to add to the team."""

    _require_manager(actor)
    query = (query or "").strip()
    if len(query) < SEARCH_MIN:
        return []
    query = query[:SEARCH_MAX]
    User = get_user_model()
    users = list(
        User.objects.filter(is_active=True)
        .filter(Q(email__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query))
        .order_by("first_name", "last_name", "email")[:SEARCH_LIMIT]
    )
    roles = dict(
        PointsAdmin.objects.filter(user__in=users, is_active=True).values_list("user_id", "role")
    )
    results = []
    for user in users:
        current = "superuser" if user.is_superuser else roles.get(user.pk)
        results.append({**_account(user), "role": current, "role_label": ROLE_LABELS.get(current) if current else None})
    return results


def _audit(actor, row: PointsAdmin, change: str, before: dict, after: dict) -> None:
    from crm.models import CrmAuditEvent

    CrmAuditEvent.objects.create(
        actor=actor,
        action=CrmAuditEvent.Action.EDIT,
        target_type=AUDIT_TARGET,
        target_id=str(row.pk),
        detail={"change": change, "user_id": row.user_id, "before": before, "after": after},
    )
    logger.info(
        "admin_team.%s actor=%s row=%s user=%s before=%s after=%s",
        change, getattr(actor, "pk", None), row.pk, row.user_id, before, after,
    )


def _snapshot(row: PointsAdmin) -> dict:
    return {"role": row.role, "team": row.portfolio, "is_active": row.is_active}


def _would_strand_admins(row: PointsAdmin) -> bool:
    """Whether losing this ``admin`` leaves nobody who can manage the team."""

    if row.role != "admin" or not row.is_active:
        return False
    User = get_user_model()
    if User.objects.filter(is_superuser=True, is_active=True).exists():
        return False
    return not (
        PointsAdmin.objects.filter(is_active=True, role="admin", user__is_active=True)
        .exclude(pk=row.pk)
        .exists()
    )


def _actor_slack_id(actor) -> str | None:
    slack_id = getattr(actor, "slack_id", None)
    return str(slack_id)[:50] if slack_id else None


def grant(actor, account_id: str, role: str, team=None) -> dict:
    """Give an account admin or committee access, reusing any row it has."""

    _require_manager(actor)
    role = _clean_role(role)
    team = _clean_team(team)
    try:
        profile_id = uuid.UUID(str(account_id))
    except (TypeError, ValueError):
        raise TeamError("not_found", 404) from None
    User = get_user_model()
    user = User.objects.filter(community_chat_profile_id=profile_id, is_active=True).first()
    if user is None:
        raise TeamError("not_found", 404)
    if actor is not None and user.pk == actor.pk:
        raise TeamError("self_change", 409)
    if user.is_superuser:
        raise TeamError("superuser_managed", 409)
    with transaction.atomic():
        row = (
            PointsAdmin.objects.select_for_update()
            .filter(user=user)
            .order_by("-is_active", "-created_at")
            .first()
        )
        if row is None and user.slack_id:
            row = PointsAdmin.objects.select_for_update().filter(slack_user_id=user.slack_id).first()
        if row is None:
            row = PointsAdmin.objects.create(
                slack_user_id=user.slack_id or f"{ACCOUNT_SLACK_PREFIX}{user.community_chat_profile_id}",
                user=user,
                role=role,
                portfolio=team,
                is_active=True,
                added_by_slack_id=_actor_slack_id(actor),
            )
            _audit(actor, row, "grant", {}, _snapshot(row))
            return _member(row, user, actor)
        before = _snapshot(row)
        if before["is_active"] and row.role == role and (team is None or row.portfolio == team):
            if row.user_id != user.pk:
                row.user = user
                row.save(update_fields=["user"])
            return _member(row, user, actor)
        if row.is_active and row.role == "admin" and role != "admin" and _would_strand_admins(row):
            raise TeamError("last_admin", 409)
        row.user = user
        row.role = role
        row.is_active = True
        if team is not None:
            row.portfolio = team
        fields = ["user", "role", "is_active", "portfolio"]
        if not before["is_active"]:
            row.added_by_slack_id = _actor_slack_id(actor)
            fields.append("added_by_slack_id")
        if (
            user.slack_id
            and row.slack_user_id.startswith(ACCOUNT_SLACK_PREFIX)
            and not PointsAdmin.objects.filter(slack_user_id=user.slack_id).exclude(pk=row.pk).exists()
        ):
            row.slack_user_id = user.slack_id
            fields.append("slack_user_id")
        row.save(update_fields=fields)
        _audit(actor, row, "grant", before, _snapshot(row))
        return _member(row, user, actor)


def _locked_row(row_id) -> PointsAdmin:
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        raise TeamError("not_found", 404) from None
    row = PointsAdmin.objects.select_for_update().select_related("user").filter(pk=row_id, is_active=True).first()
    if row is None:
        raise TeamError("not_found", 404)
    if row.user is not None and row.user.is_active and row.user.is_superuser:
        raise TeamError("superuser_managed", 409)
    return row


_UNSET = object()


def update(actor, row_id, *, role=_UNSET, team=_UNSET) -> dict:
    """Change someone's role (admin or committee) and/or team."""

    _require_manager(actor)
    if role is _UNSET and team is _UNSET:
        raise TeamError("nothing_to_change")
    if role is not _UNSET:
        role = _clean_role(role)
    if team is not _UNSET:
        team = _clean_team(team)
    with transaction.atomic():
        row = _locked_row(row_id)
        is_self = actor is not None and row.user_id == actor.pk
        before = _snapshot(row)
        fields = []
        if role is not _UNSET and role != row.role:
            if is_self:
                raise TeamError("self_change", 409)
            if row.role == "admin" and _would_strand_admins(row):
                raise TeamError("last_admin", 409)
            row.role = role
            fields.append("role")
        if team is not _UNSET and team != row.portfolio:
            row.portfolio = team
            fields.append("portfolio")
        if fields:
            row.save(update_fields=fields)
            _audit(actor, row, "update", before, _snapshot(row))
        return _member(row, row.user, actor)


def remove(actor, row_id) -> None:
    """Take someone off the team: deactivate their row, keep its history."""

    _require_manager(actor)
    with transaction.atomic():
        row = _locked_row(row_id)
        if actor is not None and row.user_id == actor.pk:
            raise TeamError("self_change", 409)
        if _would_strand_admins(row):
            raise TeamError("last_admin", 409)
        before = _snapshot(row)
        row.is_active = False
        row.save(update_fields=["is_active"])
        _audit(actor, row, "remove", before, _snapshot(row))
