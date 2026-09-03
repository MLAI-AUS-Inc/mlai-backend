from django.urls import path
from .medhack_views import CreateAnnouncementView

urlpatterns = [
    # Announcements (legacy alias for the canonical HealthHack endpoint)
    path('announcements/', CreateAnnouncementView.as_view(), name='medhack-create-announcement'),
]
