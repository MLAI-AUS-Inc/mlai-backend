from django.urls import include, path
from .views import HackathonListView, HackathonDetailView
from generic_hackathons import smart_home_views, watt_views

urlpatterns = [
    path('watt/unity-sessions/current/', watt_views.WattUnitySessionCurrentView.as_view(), name='watt-unity-session-current'),
    path('watt/unity-sessions/redeem-ticket/', watt_views.WattUnitySessionRedeemTicketView.as_view(), name='watt-unity-session-redeem-ticket'),
    path('watt/unity-sessions/dev-token/', watt_views.WattUnitySessionDevTokenView.as_view(), name='watt-unity-session-dev-token'),
    path('watt/firebase-token/', watt_views.WattParticipantFirebaseTokenView.as_view(), name='watt-participant-firebase-token'),
    path('watt/smart-home/blocks/', smart_home_views.SmartHomeBlocksView.as_view(), name='watt-smart-home-blocks'),
    path('watt/smart-home/deploy/', smart_home_views.SmartHomeDeployView.as_view(), name='watt-smart-home-deploy'),
    path('watt/smart-home/state/', smart_home_views.SmartHomeStateView.as_view(), name='watt-smart-home-state'),
    path('watt/smart-home/shop/', smart_home_views.SmartHomeShopView.as_view(), name='watt-smart-home-shop'),
    path('watt/smart-home/buy/', smart_home_views.SmartHomeBuyView.as_view(), name='watt-smart-home-buy'),
    path('<slug:slug>/app/', include('generic_hackathons.urls')),
    path('', HackathonListView.as_view(), name='hackathon-list'),
    path('<slug:slug>/', HackathonDetailView.as_view(), name='hackathon-detail'),
]
