from django.urls import path
from .medhack_views import (
    CurrentCaseView,
    StartCaseView,
    UserCaseStatusView,
    PendingGuessView,
    SubmitGuessView,
    RecordWinnerView,
    CaseHistoryView,
)

urlpatterns = [
    # Cases
    path('cases/current/', CurrentCaseView.as_view(), name='medhack-current-case'),
    path('cases/start/', StartCaseView.as_view(), name='medhack-start-case'),
    path('cases/active/user/<str:slack_user_id>/', UserCaseStatusView.as_view(), name='medhack-user-case-status'),
    path('cases/<int:case_id>/winners/', RecordWinnerView.as_view(), name='medhack-record-winner'),
    path('cases/history/', CaseHistoryView.as_view(), name='medhack-case-history'),

    # Guesses
    path('guesses/pending/', PendingGuessView.as_view(), name='medhack-pending-guess'),
    path('guesses/submit/', SubmitGuessView.as_view(), name='medhack-submit-guess'),
]
