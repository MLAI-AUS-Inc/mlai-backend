"""Staff Admin pilot, using Chat authentication and bounded read projections."""
from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from mentoring.service import MentoringError
from .mentoring_views import MentoringBaseView
from .staff_admin_permissions import viewer_role, sections_for, ROLES, ROLE_LABELS, ROLE_TITLES
from . import staff_admin_service as service


class StaffAdminView(MentoringBaseView):
    section = 'overview'
    community_chat_throttle_scope = 'community_chat_staff_admin'

    def initial(self, request, *args, **kwargs):
        if not getattr(settings, 'STAFF_ADMIN_PANEL_ENABLED', False):
            raise MentoringError('staff_admin_disabled', 'Staff Admin is not available.', 404)
        # Keep the mentoring auth/throttle/no-store/error pattern without requiring its flag.
        APIView.initial(self, request, *args, **kwargs)
        self.admin_role = viewer_role(request)
        self.sections = sections_for(self.admin_role)
        if self.section not in self.sections:
            raise MentoringError('staff_admin_required', 'This admin section is not available to your role.', 403)


class StaffAdminAccessView(StaffAdminView):
    def get(self, request):
        index = ROLES.index(self.admin_role)
        return Response({'role': self.admin_role, 'label': ROLE_LABELS[index],
                         'title': ROLE_TITLES[index], 'sections': self.sections})


class StaffAdminOverviewView(StaffAdminView):
    def get(self, request):
        return Response(service.overview(self.sections))


class StaffAdminMembersView(StaffAdminView):
    section = 'members'

    def get(self, request):
        return Response(service.members(request.query_params))


class StaffAdminMemberView(StaffAdminMembersView):
    def get(self, request, member_id):
        return Response(service.member_detail(member_id))


class StaffAdminRooView(StaffAdminView):
    section = 'roo'

    def get(self, request):
        return Response(service.roo(request.query_params))


class StaffAdminStartupsView(StaffAdminView):
    section = 'startups'

    def get(self, request):
        return Response(service.startups(request.query_params))


class StaffAdminRolesView(StaffAdminView):
    section = 'roles'

    def get(self, request):
        return Response({'roles': [dict(id=role, label=ROLE_LABELS[i], title=ROLE_TITLES[i],
                                       sections=sections_for(role)) for i, role in enumerate(ROLES)],
                         'read_only': True})
