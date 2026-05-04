"""
Points permissions helper module.
Provides authorization checks for points system operations.
"""
from typing import Optional
from functools import wraps
from django.conf import settings
from .models import PointsAdmin


POINTS_SUPER_ADMIN_SLACK_ID = "U05QPB483K9"
FULL_POINTS_ADMIN_ROLES = ("admin", "committee", "portfolio_lead")
COWORKING_REPORT_ROLES = (*FULL_POINTS_ADMIN_ROLES, "partner")


def _clean_slack_id(slack_id: str) -> str:
    return str(slack_id or "").strip()


def _is_bootstrap_admin(slack_id: str) -> bool:
    bootstrap_admins = getattr(settings, 'POINTS_BOOTSTRAP_ADMIN_SLACK_IDS', [])
    return slack_id in bootstrap_admins


def _active_admin_with_role_exists(slack_id: str, allowed_roles: tuple[str, ...]) -> bool:
    return PointsAdmin.objects.filter(
        slack_user_id=slack_id,
        is_active=True,
        role__in=allowed_roles,
    ).exists()


def is_points_admin(slack_id: str) -> bool:
    """
    Check if a Slack user ID is an active full Points Admin.
    
    Args:
        slack_id: The Slack user ID to check
        
    Returns:
        True if the user is an active full admin, False otherwise
    """
    slack_id = _clean_slack_id(slack_id)
    if not slack_id:
        return False
    
    # Check bootstrap admins first (always active)
    if _is_bootstrap_admin(slack_id):
        return True
    
    # Check database
    return _active_admin_with_role_exists(slack_id, FULL_POINTS_ADMIN_ROLES)


def can_generate_coworking_reports(slack_id: str) -> bool:
    """
    Check if a Slack user ID can generate coworking reports.

    Partners are report-only admins: they can access coworking reports but
    cannot award points, approve requests, manage rewards, or administer tasks.
    """
    slack_id = _clean_slack_id(slack_id)
    if not slack_id:
        return False

    if _is_bootstrap_admin(slack_id):
        return True

    return _active_admin_with_role_exists(slack_id, COWORKING_REPORT_ROLES)


def is_points_super_admin(slack_id: str) -> bool:
    """Return True only for the Roo points super-admin requester."""
    return bool(slack_id and slack_id.strip() == POINTS_SUPER_ADMIN_SLACK_ID)


def get_admin_role(slack_id: str) -> Optional[str]:
    """
    Get the role of a Points Admin by Slack ID.
    
    Args:
        slack_id: The Slack user ID
        
    Returns:
        Role string ('admin', 'committee', 'portfolio_lead', 'partner') or None if not admin
    """
    slack_id = _clean_slack_id(slack_id)
    if not slack_id:
        return None
    
    # Bootstrap admins are always 'admin' role
    if _is_bootstrap_admin(slack_id):
        return 'admin'
    
    try:
        admin = PointsAdmin.objects.get(slack_user_id=slack_id, is_active=True)
        return admin.role
    except PointsAdmin.DoesNotExist:
        return None


def require_admin(func):
    """
    Decorator to enforce admin-only access for service functions.
    
    The decorated function must accept `requester_slack_id` as a keyword argument.
    Raises PermissionError if the requester is not an admin.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        requester_slack_id = kwargs.get('requester_slack_id')
        if not requester_slack_id:
            raise PermissionError("requester_slack_id is required")
        
        if not is_points_admin(requester_slack_id):
            raise PermissionError(f"User {requester_slack_id} is not authorized for this action")
        
        return func(*args, **kwargs)
    
    return wrapper


class PermissionDeniedError(Exception):
    """Raised when a user attempts an action they're not authorized for."""
    pass


class InsufficientBalanceError(Exception):
    """Raised when a user doesn't have enough points for an operation."""
    pass
