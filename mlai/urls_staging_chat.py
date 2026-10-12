"""Staging has no production root URLs, admin, integration or account mutation API."""
from django.http import JsonResponse
from django.urls import include,path


def live(request):
    return JsonResponse({'status':'ok','environment':'chat-staging'})


urlpatterns=[path('healthz/live',live),
             path('api/v1/community-chat/',include('community_chat.staging_urls'))]
