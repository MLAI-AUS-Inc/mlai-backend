from django.urls import include, path
from .views import HackathonListView, HackathonDetailView

urlpatterns = [
    path('<slug:slug>/app/', include('generic_hackathons.urls')),
    path('', HackathonListView.as_view(), name='hackathon-list'),
    path('<slug:slug>/', HackathonDetailView.as_view(), name='hackathon-detail'),
]
