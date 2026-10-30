"""Contacts and blasts API for MLAI admins and committee; thin views over ``crm``.

Every endpoint except ``access/`` answers 403 ``not_authorised`` to anyone who
is not an MLAI admin on a verified installation (``MlaiAdminView``), whatever
the flag says, and then 404 ``crm_disabled`` to admins while
``COMMUNITY_CHAT_CRM_ENABLED`` is off. Reads are not audited:
``CrmAuditEvent`` has no read action yet.
"""

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from community_chat.admin_views import MlaiAdminView
from crm import queries, sending_domains
from crm.access import channel_availability, crm_access, crm_enabled
from crm.blasts import BlastError, blast_audience, cancel_blast, queue_blast, send_tests
from crm.contact_import import (
    TARGETS,
    ImportChunkError,
    ImportFileError,
    apply_upload_chunk,
    cancel_upload_import,
    expire_stale_uploads,
    finish_upload_import,
    plan_columns,
    start_upload_import,
)
from crm.listmonk import ListmonkError
from crm.merge_tags import UnsupportedMergeTag
from crm.models import (
    Blast,
    Channel,
    Contact,
    ContactImport,
    CrmEvent,
    MessageTemplate,
    Segment,
    SenderMailbox,
)
from crm.segments import (
    SegmentError,
    catalog,
    definition_q,
    evaluate,
    quick_segment_definition,
    validate_definition,
)

from . import serializers as wire
from .errors import CrmError
from .services import (
    create_contact,
    delete_segment,
    save_blast_draft,
    save_mailbox,
    save_segment,
    stop_messaging,
    update_contact,
)

PREVIEW_SAMPLE_SIZE = 20
EVENT_BLAST_LIMIT = 50
_CHUNK_STATUSES = {"import_not_running": 409, "start_row_mismatch": 409}


def validated(serializer_class, request, *, partial=False):
    serializer = serializer_class(data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def pagination_window(request):
    """Validate a bounded page while keeping its offset explicit."""
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
        limit = min(50, max(1, int(request.query_params.get("limit", 20))))
    except (TypeError, ValueError) as exc:
        raise CrmError("invalid_pagination") from exc
    return offset, limit


def paginated(request, queryset, convert_page, *, with_count=False):
    """One page plus a same-path ``next``; ``count`` only on the first page."""
    offset, limit = pagination_window(request)
    window = list(queryset[offset : offset + limit + 1])
    body = {"results": convert_page(window[:limit])}
    query = request.query_params.copy()
    query["offset"], query["limit"] = str(offset + limit), str(limit)
    body["next"] = f"?{query.urlencode()}" if len(window) > limit else None
    if with_count and offset == 0:
        body["count"] = queryset.count()
    return body


def error_body(code, **extra):
    return {"error": code, "outcome": code, **extra}


class CrmView(MlaiAdminView):
    """The MLAI admin gate, then the CRM flag, then CRM's typed errors."""

    community_chat_throttle_scope = "community_chat_crm"

    def initial(self, request, *args, **kwargs):
        # The admin check runs first, so non-admins get 403 whatever the flag says.
        super().initial(request, *args, **kwargs)
        if self.admin_only and not crm_enabled():
            raise CrmError("crm_disabled", 404)

    def handle_exception(self, exc):
        if isinstance(exc, (CrmError, BlastError)):
            return Response(error_body(exc.code, **exc.extra), status=exc.status)
        if isinstance(exc, UnsupportedMergeTag):
            return Response(error_body(exc.code, tags=list(exc.tags)), status=exc.status)
        if isinstance(exc, ListmonkError):
            if exc.transient or exc.credentials_rejected:
                return Response(error_body("listmonk_unavailable", detail=exc.code), status=503)
            return Response(error_body("listmonk_rejected", detail=exc.code), status=502)
        if isinstance(exc, SegmentError):
            extra = {"pointer": exc.pointer, "reason": exc.reason} if exc.pointer else {}
            return Response(error_body(exc.code, **extra), status=400)
        if isinstance(exc, ImportFileError):
            return Response(error_body(exc.code), status=400)
        if isinstance(exc, ImportChunkError):
            return Response(
                error_body(exc.code, **exc.detail), status=_CHUNK_STATUSES.get(exc.code, 400)
            )
        if isinstance(exc, ValidationError):
            return Response(error_body("invalid_request", fields=exc.get_codes()), status=400)
        if isinstance(exc, (Http404, ObjectDoesNotExist)):
            return Response(error_body("not_found"), status=404)
        if isinstance(exc, IntegrityError):
            return Response(error_body("conflict"), status=409)
        return super().handle_exception(exc)


def event_summaries(events, *, now):
    """Summaries for events annotated by ``queries.events_with_last_blast``."""
    stats = queries.event_stats(events, now=now)
    ids = [event.last_blast_id for event in events if getattr(event, "last_blast_id", None)]
    blasts = {
        blast.pk: blast
        for blast in Blast.objects.filter(pk__in=ids).select_related("whatsapp_template")
    }
    return [
        wire.event_summary(
            event, stats[event.pk], blasts.get(getattr(event, "last_blast_id", None))
        )
        for event in events
    ]


def blast_summaries(blasts):
    stats = queries.blast_stats(blasts)
    return [wire.blast_summary(blast, stats.get(blast.pk)) for blast in blasts]


BLAST_RELATIONS = ("event", "segment", "created_by", "whatsapp_template", "mailbox")


def blast_detail(blast_id):
    blast = get_object_or_404(Blast.objects.select_related(*BLAST_RELATIONS), pk=blast_id)
    return wire.blast_detail(blast, queries.blast_stats([blast]).get(blast.pk))


def contact_detail(contact_id, now):
    contact = get_object_or_404(
        queries.with_marketing_eligible(Contact.objects.select_related("user"), now),
        pk=contact_id,
    )
    return wire.contact_detail(
        contact,
        now=now,
        registrations=queries.contact_registrations(contact),
        deliveries=queries.contact_deliveries(contact),
        replies=queries.contact_replies(contact),
        history=queries.contact_history(contact),
    )


def contact_rows(contacts):
    return [wire.contact_row(contact) for contact in contacts]


class AccessView(CrmView):
    """Always 200 for a signed-in account, so the client can hide the tab quietly."""

    admin_only = False

    def get(self, request):
        access = crm_access(request)
        body = {**access, "luma_sync": None, "channels": None}
        if access["can_view"]:
            sync = queries.luma_sync_status()
            body["luma_sync"] = {**sync, "last_synced_at": wire.iso(sync["last_synced_at"])}
            body["channels"] = channel_availability()
        return Response(body)


class OverviewView(CrmView):
    """The Admin overview: contacts, segments, email over 30 days, the next event and recent activity."""

    def get(self, request):
        now = timezone.now()
        event = queries.next_event(now)
        last_blast = queries.latest_sent_blast()
        activity, blasts = queries.recent_activity()
        return Response(
            {
                "contacts": queries.contact_overview(now),
                "next_event": event_summaries([event], now=now)[0] if event else None,
                "last_blast": blast_summaries([last_blast])[0] if last_blast else None,
                "segments": queries.segment_overview(now),
                "email_30d": queries.email_overview(now),
                "recent_activity": [wire.activity_row(row, blasts) for row in activity],
            }
        )


class EventsView(CrmView):
    def get(self, request):
        now = timezone.now()
        events = queries.events_queryset(
            wire.event_scope(request.query_params),
            now=now,
            text=request.query_params.get("q") or "",
        )
        return Response(
            paginated(request, events, lambda page: event_summaries(page, now=now))
        )


class EventDetailView(CrmView):
    def get(self, request, event_id):
        now = timezone.now()
        event = get_object_or_404(queries.events_with_last_blast(), pk=event_id)
        blasts = list(
            Blast.objects.filter(event=event)
            .select_related("event", "segment", "created_by", "whatsapp_template")
            .order_by("-created_at")[:EVENT_BLAST_LIMIT]
        )
        [summary] = event_summaries([event], now=now)
        return Response(
            {
                **summary,
                "blasts": blast_summaries(blasts),
                "quick_segments": wire.quick_segments(
                    event, summary["counts"], quick_segment_definition
                ),
            }
        )


class EventGuestsView(CrmView):
    def get(self, request, event_id):
        event = get_object_or_404(CrmEvent, pk=event_id)
        guests = queries.event_guests(
            event,
            wire.guest_status(request.query_params),
            now=timezone.now(),
            text=request.query_params.get("q") or "",
        )
        return Response(
            paginated(
                request,
                guests,
                lambda page: [wire.guest_row(guest) for guest in page],
                with_count=True,
            )
        )


class ContactsView(CrmView):
    def get(self, request):
        now = timezone.now()
        filters = wire.contact_list_filters(request.query_params)
        segment_id = filters.pop("segment_id")
        segment_q = None
        if segment_id:
            segment = Segment.objects.filter(pk=segment_id).first()
            if segment is None:
                raise CrmError("segment_not_found", 404)
            segment_q = definition_q(validate_definition(segment.definition), now=now)
        contacts = queries.contacts_queryset(now=now, segment_q=segment_q, **filters)
        return Response(paginated(request, contacts, contact_rows, with_count=True))

    def post(self, request):
        now = timezone.now()
        data = validated(wire.ContactCreateInput, request)
        contact = create_contact(data, actor=request.user, now=now)
        return Response(contact_detail(contact.pk, now), status=201)


class ContactDetailView(CrmView):
    def get(self, request, contact_id):
        return Response(contact_detail(contact_id, timezone.now()))

    def patch(self, request, contact_id):
        now = timezone.now()
        data = validated(wire.ContactFieldsInput, request, partial=True)
        update_contact(contact_id, data, actor=request.user, now=now)
        return Response(contact_detail(contact_id, now))


class StopMessagingView(CrmView):
    def post(self, request, contact_id):
        now = timezone.now()
        data = validated(wire.StopMessagingInput, request)
        stop_messaging(
            contact_id, data["channels"], reason=data["reason"], actor=request.user, now=now
        )
        return Response(contact_detail(contact_id, now))


class TagsView(CrmView):
    def get(self, request):
        return Response({"results": queries.tag_counts()})


class ImportPlanView(CrmView):
    def post(self, request):
        headers = validated(wire.ImportPlanInput, request)["headers"]
        plan = plan_columns(headers, require_email=False)
        return Response(
            {
                "columns": [{"header": header, "target": plan[header]} for header in headers],
                "targets": list(TARGETS),
                "has_email": "email" in plan.values(),
            }
        )


class ImportsView(CrmView):
    def get(self, request, import_id=None):
        expire_stale_uploads()
        if import_id is not None:
            return Response(wire.import_record(get_object_or_404(ContactImport, pk=import_id)))
        records = ContactImport.objects.order_by("-created_at", "-pk")
        return Response(
            paginated(request, records, lambda page: [wire.import_record(item) for item in page])
        )

    def post(self, request):
        data = validated(wire.ImportCreateInput, request)
        record = start_upload_import(
            file_name=data["file_name"],
            consent=data["consent"],
            consent_source=data["consent_source"],
            tags=data["tags"],
            column_plan=data["mapping"],
            row_count=data["row_count"],
            dry_run=data["dry_run"],
            actor=request.user,
        )
        return Response(wire.import_record(record), status=201)


class ImportRowsView(CrmView):
    def post(self, request, import_id):
        rows = request.data.get("rows") if isinstance(request.data, dict) else None
        if isinstance(rows, list) and len(rows) > 1000:
            raise CrmError("too_many_rows", limit=1000)
        data = validated(wire.ImportRowsInput, request)
        record = apply_upload_chunk(
            import_id, start_row=data["start_row"], rows=data["rows"], actor=request.user
        )
        return Response(wire.import_record(record))


class ImportFinishView(CrmView):
    def post(self, request, import_id):
        return Response(wire.import_record(finish_upload_import(import_id, actor=request.user)))


class ImportCancelView(CrmView):
    def post(self, request, import_id):
        get_object_or_404(ContactImport, pk=import_id)
        return Response(wire.import_record(cancel_upload_import(import_id, actor=request.user)))


class SegmentsView(CrmView):
    def get(self, request, segment_id=None):
        if segment_id is not None:
            return Response(wire.segment_dto(get_object_or_404(Segment, pk=segment_id)))
        segments = Segment.objects.order_by("name", "pk")
        return Response(
            paginated(request, segments, lambda page: [wire.segment_dto(item) for item in page])
        )

    def post(self, request):
        data = validated(wire.SegmentInput, request)
        segment = save_segment(None, data, actor=request.user, now=timezone.now())
        return Response(wire.segment_dto(segment), status=201)

    def patch(self, request, segment_id):
        segment = get_object_or_404(Segment, pk=segment_id)
        data = validated(wire.SegmentInput, request, partial=True)
        segment = save_segment(segment, data, actor=request.user, now=timezone.now())
        return Response(wire.segment_dto(segment))

    def delete(self, request, segment_id):
        delete_segment(get_object_or_404(Segment, pk=segment_id), actor=request.user)
        return Response(status=204)


class SegmentPreviewView(CrmView):
    def post(self, request):
        now = timezone.now()
        data = validated(wire.SegmentPreviewInput, request)
        event = None
        if data["event_id"] is not None:
            event = CrmEvent.objects.filter(pk=data["event_id"]).first()
            if event is None:
                raise CrmError("event_not_found", 404)
        audience, excluded = evaluate(data["definition"], data["purpose"], event, now=now)
        sample = queries.sort_contacts(queries.with_marketing_eligible(audience, now), "name")
        return Response(
            {
                "count": audience.count(),
                "sample": contact_rows(sample[:PREVIEW_SAMPLE_SIZE]),
                "excluded": excluded,
            }
        )


class SegmentCatalogView(CrmView):
    def get(self, request):
        return Response(
            catalog(
                tags=queries.tag_counts(),
                attributes=queries.attribute_key_counts(),
                questions=queries.registration_question_counts(),
            )
        )


def mailbox_response(mailbox, *, status=200):
    sent = queries.mailbox_sent_last_24h([mailbox.pk], timezone.now())
    return Response(wire.mailbox_dto(mailbox, sent.get(mailbox.pk, 0)), status=status)


class MailboxesView(CrmView):
    def get(self, request, mailbox_id=None):
        if mailbox_id is not None:
            return mailbox_response(get_object_or_404(SenderMailbox, pk=mailbox_id))
        mailboxes = list(SenderMailbox.objects.order_by("display_name", "email", "pk"))
        sent = queries.mailbox_sent_last_24h([mailbox.pk for mailbox in mailboxes], timezone.now())
        return Response(
            {"results": [wire.mailbox_dto(mailbox, sent.get(mailbox.pk, 0)) for mailbox in mailboxes]}
        )

    def post(self, request):
        mailbox = save_mailbox(None, validated(wire.MailboxCreateInput, request), actor=request.user)
        return mailbox_response(mailbox, status=201)

    def patch(self, request, mailbox_id):
        mailbox = get_object_or_404(SenderMailbox, pk=mailbox_id)
        data = validated(wire.MailboxUpdateInput, request, partial=True)
        return mailbox_response(save_mailbox(mailbox, data, actor=request.user))


class SendingDomainsView(CrmView):
    """Listed sending domains with their DNS checks; ``?refresh=1`` skips the cache."""

    def get(self, request):
        refresh = request.query_params.get("refresh") in ("1", "true")
        configured = sending_domains.configured_domains()
        counts = dict(
            SenderMailbox.objects.values_list("domain").annotate(total=Count("pk")).order_by()
        )
        now = timezone.now()
        results = [
            wire.sending_domain_dto(
                domain,
                route,
                sending_domains.domain_health(domain, route, refresh=refresh, now=now),
                counts.get(domain, 0),
            )
            for domain, route in configured.items()
        ]
        results += [
            wire.sending_domain_dto(domain, None, None, total)
            for domain, total in sorted(counts.items())
            if domain not in configured
        ]
        return Response({"results": results})


class WhatsAppTemplatesView(CrmView):
    """Approved, enabled templates only: WhatsApp allows no business-started free text."""

    def get(self, request):
        templates = MessageTemplate.objects.filter(
            channel=Channel.WHATSAPP, approved=True, enabled=True
        ).order_by("name", "pk")
        return Response({"results": [wire.template_dto(template) for template in templates]})


class BlastsView(CrmView):
    def get(self, request, blast_id=None):
        if blast_id is not None:
            return Response(blast_detail(blast_id))
        blasts = queries.blasts_queryset(**wire.blast_list_filters(request.query_params))
        return Response(paginated(request, blasts, blast_summaries))

    def post(self, request):
        data = validated(wire.BlastInput, request)
        blast = save_blast_draft(None, data, actor=request.user, now=timezone.now())
        return Response(blast_detail(blast.pk), status=201)

    def patch(self, request, blast_id):
        blast = get_object_or_404(Blast.objects.select_related("event", "segment"), pk=blast_id)
        data = validated(wire.BlastInput, request, partial=True)
        save_blast_draft(blast, data, actor=request.user, now=timezone.now())
        return Response(blast_detail(blast_id))


class BlastAudienceView(CrmView):
    def post(self, request, blast_id):
        blast = get_object_or_404(
            Blast.objects.select_related("event", "mailbox", "segment"), pk=blast_id
        )
        return Response(blast_audience(blast))


class BlastTestView(CrmView):
    def post(self, request, blast_id):
        data = validated(wire.BlastTestInput, request)
        return Response(send_tests(blast_id, data["channels"], user=request.user))


class BlastSendView(CrmView):
    def post(self, request, blast_id):
        data = validated(wire.BlastSendInput, request)
        blast = queue_blast(
            blast_id,
            confirm=data["confirm"],
            idempotency_key=data["idempotency_key"],
            actor=request.user,
        )
        return Response(blast_detail(blast.pk), status=202)


class BlastCancelView(CrmView):
    def post(self, request, blast_id):
        cancel_blast(blast_id, actor=request.user)
        return Response(blast_detail(blast_id))


class BlastDeliveriesView(CrmView):
    def get(self, request, blast_id):
        blast = get_object_or_404(Blast, pk=blast_id)
        deliveries = queries.blast_deliveries(blast, **wire.delivery_filters(request.query_params))
        return Response(
            paginated(
                request,
                deliveries,
                lambda page: [wire.delivery_row(delivery) for delivery in page],
                with_count=True,
            )
        )


class BlastRepliesView(CrmView):
    def get(self, request, blast_id):
        blast = get_object_or_404(Blast, pk=blast_id)
        return Response(
            paginated(
                request,
                queries.blast_replies(blast),
                lambda page: [wire.reply_row(message) for message in page],
            )
        )
