# app/urls.py
from django.urls import path
from . import views


urlpatterns = [
    path('submit_predictions/', views.submit_predictions, name='submit_predictions'),
    path('get_submission/', views.get_submission, name='get_submission'),
    path('get_recent_submissions/', views.get_recent_submissions, name='get_recent_submissions'),
    path('get_submission/<int:submission_id>/', views.get_submission_by_id, name='get_submission_by_id'),
    # path('get_leaderboard_submissions/', views.get_leaderboard_submissions, name='get_leaderboard_submissions'),
    path('get_team_names/', views.get_team_names, name='get_team_names'),
]
