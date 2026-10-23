"""Contacts API for Chat owners and admins; thin views over ``crm``.

Every endpoint except ``access/`` answers 404 ``crm_disabled`` while
``COMMUNITY_CHAT_CRM_ENABLED`` is off and 403 ``not_authorised`` to anyone who
is not a Chat owner or admin on a verified installation. Reads are not
audited: ``CrmAuditEvent`` has no read action yet.
"""

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.authentication import CustomJWTAuthentication
from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.throttles import CommunityChatScopedThrottle
from crm import queries
from crm.access import channel_availability, crm_access, crm_enabled, is_crm_admin
from crm.contact_import import (
    TARGETS,
    ImportChunkError,
    ImportFileError,
    apply_upload_chunk,
    finish_upload_import,
    plan_columns,
    start_upload_import,
)
from crm.models import Blast, Contact, ContactImport, CrmEvent, Segment
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


class CrmView(APIView):
    """Volunteer's account-session and JWT contracts, with the CRM admin gate."""

    authentication_classes = (
        CommunityChatAccountAuthentication,
        CustomJWTAuthentication,
    )
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_crm"
    admin_only = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not self.admin_only:
            return
        if not crm_enabled():
            raise CrmError("crm_disabled", 404)
        if not is_crm_admin(request):
            raise CrmError("not_authorised", 403)

    def handle_exception(self, exc):
        if isinstance(exc, CrmError):
            return Response(error_body(exc.code, **exc.extra), status=exc.status)
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

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


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
    def get(self, request):
        now = timezone.now()
        event = queries.next_event(now)
        last_blast = queries.latest_sent_blast()
        return Response(
            {
                "contacts": queries.contact_overview(now),
                "next_event": event_summaries([event], now=now)[0] if event else None,
                "last_blast": blast_summaries([last_blast])[0] if last_blast else None,
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
        plan = plan_columns(headers)
        return Response(
            {
                "columns": [{"header": header, "target": plan[header]} for header in headers],
                "targets": list(TARGETS),
            }
        )


class ImportsView(CrmView):
    def get(self, request, import_id=None):
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
        record = apply_upload_chunk(import_id, start_row=data["start_row"], rows=data["rows"])
        return Response(wire.import_record(record))


class ImportFinishView(CrmView):
    def post(self, request, import_id):
        return Response(wire.import_record(finish_upload_import(import_id, actor=request.user)))


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
