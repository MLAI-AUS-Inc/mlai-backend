"""Public signed preference actions; they never accept ordinary account identifiers."""

from django.urls import path

from .preference_views import reconfirm

urlpatterns = [path('reconfirm/', reconfirm, name='crm_reconfirm')]
