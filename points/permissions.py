"""
Points permissions helper module.
Provides authorization checks for points system operations.
"""
from functools import wraps
from django.conf import settings
from .models import PointsAdmin


def is_points_admin(slack_id: str) -> bool:
    """
    Check if a Slack user ID is an active Points Admin.
    
    Args:
        slack_id: The Slack user ID to check
        
    Returns:
        True if the user is an active admin, False otherwise
    """
    if not slack_id:
        return False
    
    # Check bootstrap admins first (always active)
    bootstrap_admins = getattr(settings, 'POINTS_BOOTSTRAP_ADMIN_SLACK_IDS', [])
    if slack_id in bootstrap_admins:
        return True
    
    # Check database
    return PointsAdmin.objects.filter(
        slack_user_id=slack_id,
        is_active=True
    ).exists()


def get_admin_role(slack_id: str) -> str | None:
    """
    Get the role of a Points Admin by Slack ID.
    
    Args:
        slack_id: The Slack user ID
        
    Returns:
        Role string ('admin', 'committee', 'portfolio_lead') or None if not admin
    """
    if not slack_id:
        return None
    
    # Bootstrap admins are always 'admin' role
    bootstrap_admins = getattr(settings, 'POINTS_BOOTSTRAP_ADMIN_SLACK_IDS', [])
    if slack_id in bootstrap_admins:
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
