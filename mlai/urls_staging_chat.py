"""Environment auth overrides followed by the authoritative normal route tree."""
from django.http import JsonResponse
from django.urls import include,path


def live(request):
    return JsonResponse({'status':'ok','environment':'chat-staging'})


urlpatterns=[path('healthz/live',live),
             path('api/v1/community-chat/',include('community_chat.staging_urls')),
             path('api/v1/points/',include('roo.staging_urls')),
    path('',include('mlai.urls'))]
