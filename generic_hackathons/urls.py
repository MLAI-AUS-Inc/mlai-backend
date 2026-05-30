from django.urls import path

from . import views


urlpatterns = [
    path('teams/', views.GenericHackathonTeamListCreateView.as_view(), name='generic-hackathon-teams'),
    path('team/', views.GenericHackathonCurrentTeamView.as_view(), name='generic-hackathon-current-team'),
    path('teams/join/', views.GenericHackathonJoinTeamView.as_view(), name='generic-hackathon-join-team'),
    path('submissions/', views.GenericHackathonSubmissionListCreateView.as_view(), name='generic-hackathon-submissions'),
    path('submissions/<int:submission_id>/', views.GenericHackathonSubmissionDetailView.as_view(), name='generic-hackathon-submission-detail'),
    path('announcements/', views.GenericHackathonAnnouncementListView.as_view(), name='generic-hackathon-announcements'),
    path('resources/', views.GenericHackathonResourceListView.as_view(), name='generic-hackathon-resources'),
]
