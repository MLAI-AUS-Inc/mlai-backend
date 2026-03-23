from django.urls import path
from . import views

urlpatterns = [
    path('link-slack/', views.LinkSlackView.as_view(), name='link_slack'),
    path('slack-user/', views.GetOrCreateSlackUserView.as_view(), name='get_or_create_slack_user'),
]
