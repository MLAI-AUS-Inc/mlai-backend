"""Contacts routes under the CORS-allowed ``/api/v1/community-chat/`` prefix."""

from django.urls import path

from . import views

urlpatterns = [
    path("access/", views.AccessView.as_view(), name="community_chat_crm_access"),
    path("overview/", views.OverviewView.as_view(), name="community_chat_crm_overview"),
    path("events/", views.EventsView.as_view(), name="community_chat_crm_events"),
    path("events/<uuid:event_id>/", views.EventDetailView.as_view(), name="community_chat_crm_event"),
    path(
        "events/<uuid:event_id>/guests/",
        views.EventGuestsView.as_view(),
        name="community_chat_crm_event_guests",
    ),
    path("contacts/", views.ContactsView.as_view(), name="community_chat_crm_contacts"),
    path(
        "contacts/<uuid:contact_id>/",
        views.ContactDetailView.as_view(),
        name="community_chat_crm_contact",
    ),
    path(
        "contacts/<uuid:contact_id>/stop-messaging/",
        views.StopMessagingView.as_view(),
        name="community_chat_crm_contact_stop_messaging",
    ),
    path("tags/", views.TagsView.as_view(), name="community_chat_crm_tags"),
    path("imports/plan/", views.ImportPlanView.as_view(), name="community_chat_crm_import_plan"),
    path("imports/", views.ImportsView.as_view(), name="community_chat_crm_imports"),
    path("imports/<uuid:import_id>/", views.ImportsView.as_view(), name="community_chat_crm_import"),
    path(
        "imports/<uuid:import_id>/rows/",
        views.ImportRowsView.as_view(),
        name="community_chat_crm_import_rows",
    ),
    path(
        "imports/<uuid:import_id>/finish/",
        views.ImportFinishView.as_view(),
        name="community_chat_crm_import_finish",
    ),
    path("segments/", views.SegmentsView.as_view(), name="community_chat_crm_segments"),
    path(
        "segments/preview/",
        views.SegmentPreviewView.as_view(),
        name="community_chat_crm_segment_preview",
    ),
    path(
        "segments/catalog/",
        views.SegmentCatalogView.as_view(),
        name="community_chat_crm_segment_catalog",
    ),
    path(
        "segments/<uuid:segment_id>/",
        views.SegmentsView.as_view(),
        name="community_chat_crm_segment",
    ),
]
