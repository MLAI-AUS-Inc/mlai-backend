from django.urls import path

from . import views


urlpatterns = [
    path('teams/', views.GenericHackathonTeamListCreateView.as_view(), name='generic-hackathon-teams'),
    path('team/', views.GenericHackathonCurrentTeamView.as_view(), name='generic-hackathon-current-team'),
    path('teams/join/', views.GenericHackathonJoinTeamView.as_view(), name='generic-hackathon-join-team'),
    path('team/leave/', views.GenericHackathonLeaveTeamView.as_view(), name='generic-hackathon-leave-team'),
    path('team/transfer-lead/', views.GenericHackathonTransferLeadView.as_view(), name='generic-hackathon-transfer-lead'),
    path('team/disband/', views.GenericHackathonDisbandTeamView.as_view(), name='generic-hackathon-disband-team'),
    path('team/requests/', views.GenericHackathonJoinRequestsView.as_view(), name='generic-hackathon-join-requests'),
    path('team/requests/<int:request_id>/accept/', views.GenericHackathonAcceptRequestView.as_view(), name='generic-hackathon-accept-request'),
    path('team/requests/<int:request_id>/reject/', views.GenericHackathonRejectRequestView.as_view(), name='generic-hackathon-reject-request'),
    path('team/requests/<int:request_id>/cancel/', views.GenericHackathonCancelRequestView.as_view(), name='generic-hackathon-cancel-request'),
    path('submissions/', views.GenericHackathonSubmissionListCreateView.as_view(), name='generic-hackathon-submissions'),
    path('submissions/<int:submission_id>/', views.GenericHackathonSubmissionDetailView.as_view(), name='generic-hackathon-submission-detail'),
    path('announcements/', views.GenericHackathonAnnouncementListView.as_view(), name='generic-hackathon-announcements'),
    path('resources/', views.GenericHackathonResourceListView.as_view(), name='generic-hackathon-resources'),
]
