"""Public post-session feedback links (see mentoring.feedback_views)."""

from django.urls import path

from .feedback_views import feedback_comment, feedback_page, feedback_unsubscribe

urlpatterns = [
    path('<str:token>/', feedback_page, name='mentoring_feedback_page'),
    path('<str:token>/comment/', feedback_comment, name='mentoring_feedback_comment'),
    path('<str:token>/unsubscribe/', feedback_unsubscribe, name='mentoring_feedback_unsubscribe'),
]
