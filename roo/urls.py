"""
URL routing for Roo Slack bot.
"""
from django.urls import path
from .views import SlackEventsView, SlackCommandsView, HealthCheckView

urlpatterns = [
    # Health check
    path('health/', HealthCheckView.as_view(), name='roo-health'),
    
    # Slack Events API webhook
    path('events/', SlackEventsView.as_view(), name='slack-events'),
    
    # Slack Slash Commands webhook  
    path('commands/', SlackCommandsView.as_view(), name='slack-commands'),
]
