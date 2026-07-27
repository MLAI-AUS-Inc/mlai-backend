from django.urls import path

from .public_views import PublicKnowledgeAnswerView


urlpatterns = [
    path("answer", PublicKnowledgeAnswerView.as_view(), name="public-brain-answer"),
]
