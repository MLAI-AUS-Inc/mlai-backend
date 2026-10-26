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
    path(
        "imports/<uuid:import_id>/cancel/",
        views.ImportCancelView.as_view(),
        name="community_chat_crm_import_cancel",
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
    path("mailboxes/", views.MailboxesView.as_view(), name="community_chat_crm_mailboxes"),
    path(
        "mailboxes/<uuid:mailbox_id>/",
        views.MailboxesView.as_view(),
        name="community_chat_crm_mailbox",
    ),
    path(
        "sending-domains/",
        views.SendingDomainsView.as_view(),
        name="community_chat_crm_sending_domains",
    ),
    path(
        "whatsapp-templates/",
        views.WhatsAppTemplatesView.as_view(),
        name="community_chat_crm_whatsapp_templates",
    ),
    path("blasts/", views.BlastsView.as_view(), name="community_chat_crm_blasts"),
    path("blasts/<uuid:blast_id>/", views.BlastsView.as_view(), name="community_chat_crm_blast"),
    path(
        "blasts/<uuid:blast_id>/audience/",
        views.BlastAudienceView.as_view(),
        name="community_chat_crm_blast_audience",
    ),
    path(
        "blasts/<uuid:blast_id>/test/",
        views.BlastTestView.as_view(),
        name="community_chat_crm_blast_test",
    ),
    path(
        "blasts/<uuid:blast_id>/send/",
        views.BlastSendView.as_view(),
        name="community_chat_crm_blast_send",
    ),
    path(
        "blasts/<uuid:blast_id>/cancel/",
        views.BlastCancelView.as_view(),
        name="community_chat_crm_blast_cancel",
    ),
    path(
        "blasts/<uuid:blast_id>/deliveries/",
        views.BlastDeliveriesView.as_view(),
        name="community_chat_crm_blast_deliveries",
    ),
    path(
        "blasts/<uuid:blast_id>/replies/",
        views.BlastRepliesView.as_view(),
        name="community_chat_crm_blast_replies",
    ),
]
