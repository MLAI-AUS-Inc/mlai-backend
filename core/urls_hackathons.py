from django.urls import include, path
from .views import HackathonListView, HackathonDetailView
from generic_hackathons import smart_home_views

urlpatterns = [
    path('watt/smart-home/blocks/', smart_home_views.SmartHomeBlocksView.as_view(), name='watt-smart-home-blocks'),
    path('watt/smart-home/deploy/', smart_home_views.SmartHomeDeployView.as_view(), name='watt-smart-home-deploy'),
    path('<slug:slug>/app/', include('generic_hackathons.urls')),
    path('', HackathonListView.as_view(), name='hackathon-list'),
    path('<slug:slug>/', HackathonDetailView.as_view(), name='hackathon-detail'),
]
