"""Fixed committee access over existing appointments, with no schema changes."""
from django.db.models import Prefetch
from roo.models import PointsAdmin
from .models import Moderator
from .permissions import account_chat_role

ROLES = ('superadmin', 'admin', 'finance', 'community', 'marketing', 'tech')
ROLE_LABELS = ('Superadmin', 'Admin', 'Finance', 'Community', 'Marketing and Partnerships', 'Tech')
ROLE_TITLES = ('President', 'Vice President', 'Treasurer', 'Community Manager', 'Marketing and Partnerships', 'Tech Lead')
PERMISSIONS = {
    'overview': 'VVVVVV', 'mentoring': 'EE-EVV', 'roo': 'VVVV-V',
    'members': 'EE-E-V', 'moderation': 'EE-E--', 'startups': 'EE-EVV',
    'volunteers': 'EE-EV-', 'events': 'EEVEEV', 'hackathons': 'EE-EVV',
    'jobs': 'EE---E', 'content': 'EE-VEE', 'memory': 'EV---E',
    'integrations': 'EVV--E', 'finance': 'VVE---', 'applications': 'EE-EE-',
    'audit': 'VVV--V', 'roles': 'EV----',
}


def compute_role(user, chat_role, moderator, appointments):
    """Resolve one role in explicit precedence order; never union permissions."""
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return None
    if chat_role == 'owner' or getattr(user, 'is_superuser', False):
        return 'superadmin'
    if chat_role == 'admin':
        return 'admin'
    active = [p for p in appointments if p.is_active]
    if moderator or any(p.portfolio == 'ops' for p in active):
        return 'community'
    if any(p.role == 'portfolio_lead' and p.portfolio in ('marketing', 'sales') for p in active):
        return 'marketing'
    if any(p.portfolio == 'tech' for p in active):
        return 'tech'
    if any(p.role == 'committee' and not p.portfolio for p in active):
        return 'finance'
    if getattr(user, 'is_staff', False):
        return 'tech'
    return None


def user_role(user, chat_role=None):
    """Use prefetched appointments for member lists, avoiding per-row queries."""
    if chat_role is None:
        appointment = getattr(user, 'chat_role_appointment', None)
        chat_role = appointment.role if appointment else 'member'
    moderator = getattr(user, 'chat_moderator', None)
    appointments = user.points_admin_profile.all()
    return compute_role(user, chat_role, bool(moderator and moderator.is_active), appointments)


def viewer_role(request):
    """Chat authority requires the session's verified device, as in mentoring."""
    user = request.user
    if not getattr(user, 'is_active', False):
        return None
    role = account_chat_role(user, getattr(request, 'community_chat_public_key', None),
                             getattr(request, 'community_chat_installation_id', None))
    moderator = Moderator.objects.filter(user=user, is_active=True).exists()
    return compute_role(user, role, moderator, PointsAdmin.objects.filter(user=user, is_active=True))


def sections_for(role):
    if role not in ROLES:
        return {}
    index = ROLES.index(role)
    return {section: ('edit' if row[index] == 'E' else 'view')
            for section, row in PERMISSIONS.items() if row[index] != '-'}


def with_roles(query):
    return query.select_related('chat_role_appointment', 'chat_moderator').prefetch_related(
        Prefetch('points_admin_profile', queryset=PointsAdmin.objects.filter(is_active=True)))
