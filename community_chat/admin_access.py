"""Who may use MLAI Chat's Admin area, and which of its sections are switched on.

An MLAI admin is an active account with a linked, active ``roo.PointsAdmin``
row whose role is ``admin`` or ``committee``, or a superuser
(``roo.permissions.mlai_admin_role``). The request must also come from that
account's own verified, unrevoked device installation, so JWT-only web
sessions never qualify. Chat owner and admin appointments don't count.

This "admin" is unrelated to Django's ``/admin/`` site and to the relay's
``/api/admin/v1`` console.
"""

from __future__ import annotations

from roo.permissions import is_mlai_admin_user

from .permissions import has_verified_installation


class AdminError(Exception):
    """The acting account isn't an MLAI admin; answered as 403 ``not_authorised``."""

    code = "not_authorised"
    status = 403
    extra: dict = {}

    def __init__(self):
        super().__init__(self.code)


def is_mlai_admin_request(request) -> bool:
    """An MLAI admin's session on that account's own verified installation."""

    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if not has_verified_installation(
        user,
        getattr(request, "community_chat_public_key", None),
        getattr(request, "community_chat_installation_id", None),
    ):
        return False
    return is_mlai_admin_user(user)


def admin_sections(request) -> list[str]:
    """The Admin area's switched-on sections. Add future sections here."""

    # Imported here: ``crm.access`` imports this module for its gate.
    from crm.access import crm_enabled

    return ["crm"] if crm_enabled() else []


def require_mlai_admin(user) -> None:
    """Refuse a service call whose acting account isn't an MLAI admin.

    Service write functions call this first, so a caller that skips the views
    (a command, an agent tool or a task acting for someone) is still refused.
    """

    if not is_mlai_admin_user(user):
        raise AdminError()
