from django.urls import path
from . import views_activity

urlpatterns = [
    path('first-post/', views_activity.ChannelActivityView.as_view(), name='first_post_record'),
    path('first-post/<str:slack_user_id>/<str:channel_id>/', views_activity.ChannelActivityView.as_view(), name='first_post_check'),
]
