from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import ContentFactoryRun, ContentFactoryRunStatus, Organization
from core.permissions import HasRooApiKey
from integrations.api_serializers import (
    ClassificationResultsSerializer,
    DraftResultsSerializer,
    ExtractionResultsSerializer,
    StartupProfileUpsertSerializer,
    StartupUpdateBatchQuerySerializer,
    StartupUpdateIngestSerializer,
    StartupUpdateRunCreateSerializer,
    StartupUpdateThreadHydrationSerializer,
)
from integrations.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailSyncCursor,
    GmailThreadArtifact,
    MonthlyUpdateDraft,
    StartupMetricObservation,
    StartupEvent,
)
from integrations.services.gmail import (
    hydrate_thread_artifact,
    StaleHistoryCursorError,
    six_month_backfill_window,
    sync_history_metadata_page,
    sync_message_metadata_page,
)
from integrations.services.startup_updates import (
    DEFAULT_BACKFILL_MONTHS,
    OPEN_RUN_STATUSES,
    STARTUP_UPDATE_WORKFLOW,
    bind_user_to_startup,
    build_timeline_payload,
    create_startup_update_run,
    get_default_binding_for_domain,
    resolve_or_create_profile,
    seed_startup_profile,
    upsert_monthly_update_draft,
)
from integrations.services.valley_harness import notify_valley_run_created
from integrations.utils import normalize_domain

User = get_user_model()


def _serialize_profile(profile) -> dict:
    return {
        "organization_id": profile.organization_id,
        "domain": profile.organization.domain,
        "company_aliases": profile.company_aliases,
        "domain_aliases": profile.domain_aliases,
        "product_names": profile.product_names,
        "founder_names": profile.founder_names,
        "team_names": profile.team_names,
        "investor_names": profile.investor_names,
        "investor_domains": profile.investor_domains,
        "competitor_names": profile.competitor_names,
        "competitor_domains": profile.competitor_domains,
        "customer_names": profile.customer_names,
        "customer_domains": profile.customer_domains,
        "prospect_names": profile.prospect_names,
        "prospect_domains": profile.prospect_domains,
        "positive_keywords": profile.positive_keywords,
        "negative_keywords": profile.negative_keywords,
        "kpi_definitions": profile.kpi_definitions,
        "default_currency": profile.default_currency,
        "notes": profile.notes,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
    }


def _serialize_binding(binding) -> dict:
    return {
        "id": binding.id,
        "user_id": binding.user_id,
        "organization_id": binding.organization_id,
        "google_connection_id": binding.google_connection_id,
        "role": binding.role,
        "is_default_for_gmail": binding.is_default_for_gmail,
    }


def _serialize_run(run, request) -> dict:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "current_step": run.current_step,
        "run_request": run.run_request or {},
        "step_order": run.step_order or [],
        "status_url": request.build_absolute_uri(
            reverse("content_factory_run", kwargs={"run_id": run.run_id})
        ),
    }


def _serialize_open_run(run) -> dict:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "current_step": run.current_step,
        "run_request": run.run_request or {},
        "result": run.result or {},
        "step_order": run.step_order or [],
        "step_states": {},
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _serialize_attachment(attachment) -> dict:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "part_id": attachment.part_id,
        "gmail_attachment_id": attachment.gmail_attachment_id,
        "size_bytes": attachment.size_bytes,
        "is_inline": attachment.is_inline,
        "raw_content_base64": attachment.raw_content_base64,
        "extracted_text": attachment.extracted_text,
        "extraction_status": attachment.extraction_status,
        "parse_notes": attachment.parse_notes,
        "metadata": attachment.metadata or {},
    }


def _serialize_draft(draft) -> dict:
    return {
        "id": draft.id,
        "organization_id": draft.organization_id,
        "month": draft.month.isoformat(),
        "status": draft.status,
        "title": draft.title,
        "model_name": draft.model_name,
        "groundedness_status": draft.groundedness_status,
        "structured_memo": draft.structured_memo or {},
        "rendered_markdown": draft.rendered_markdown,
        "evidence_event_ids": draft.evidence_event_ids or [],
        "evidence_metric_ids": draft.evidence_metric_ids or [],
        "carry_forward_event_ids": draft.carry_forward_event_ids or [],
        "groundedness_notes": draft.groundedness_notes or "",
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }


def _serialize_event(event) -> dict:
    return {
        "id": event.id,
        "canonical_key": event.canonical_key,
        "event_type": event.event_type,
        "title": event.title,
        "summary": event.summary,
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "month_bucket": event.month_bucket.isoformat(),
        "date_precision": event.date_precision,
        "sentiment": event.sentiment,
        "investor_importance": event.investor_importance,
        "quantitative_facts": event.quantitative_facts or [],
        "evidence_message_ids": event.evidence_message_ids or [],
        "evidence_attachment_ids": event.evidence_attachment_ids or [],
        "source_thread_ids": event.source_thread_ids or [],
        "confidence": event.confidence,
        "status": event.status,
        "needs_review": event.needs_review,
        "merge_notes": event.merge_notes or "",
    }


def _serialize_metric(metric) -> dict:
    return {
        "id": metric.id,
        "metric_key": metric.metric_key,
        "metric_name": metric.metric_name,
        "value_text": metric.value_text,
        "value_number": str(metric.value_number) if metric.value_number is not None else None,
        "unit": metric.unit,
        "observed_at": metric.observed_at.isoformat() if metric.observed_at else None,
        "period_month": metric.period_month.isoformat(),
        "confidence": metric.confidence,
        "evidence_message_ids": metric.evidence_message_ids or [],
        "evidence_attachment_ids": metric.evidence_attachment_ids or [],
        "summary": metric.summary or "",
    }


def _get_run_or_404(run_id: str) -> ContentFactoryRun:
    return get_object_or_404(
        ContentFactoryRun,
        run_id=run_id,
        workflow=STARTUP_UPDATE_WORKFLOW,
    )


def _get_org_and_binding_for_run(run: ContentFactoryRun):
    organization = get_object_or_404(Organization, domain=normalize_domain(run.domain))
    binding_id = (run.run_request or {}).get("binding_id")
    binding = get_object_or_404(
        organization.user_startup_bindings.select_related("user", "google_connection"),
        id=binding_id,
    )
    google_connection = binding.google_connection or getattr(binding.user, "google_connection", None)
    if google_connection is None:
        raise ValueError("No Google connection bound to this startup update run.")
    profile = getattr(organization, "startup_profile", None)
    if profile is None:
        _, profile = resolve_or_create_profile(domain=organization.domain)
    return organization, binding, google_connection, profile


def _update_run_step(run: ContentFactoryRun, *, step_key: str):
    if run.status == ContentFactoryRunStatus.QUEUED:
        run.status = ContentFactoryRunStatus.RUNNING
    run.current_step = step_key
    run.save(update_fields=["status", "current_step", "updated_at"])


class StartupProfileView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = StartupProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = get_object_or_404(User, pk=data["user_id"])
        organization, profile = resolve_or_create_profile(domain=data["domain"])
        profile = seed_startup_profile(profile)

        update_fields = []
        for field_name in [
            "company_aliases",
            "domain_aliases",
            "product_names",
            "founder_names",
            "team_names",
            "investor_names",
            "investor_domains",
            "competitor_names",
            "competitor_domains",
            "customer_names",
            "customer_domains",
            "prospect_names",
            "prospect_domains",
            "positive_keywords",
            "negative_keywords",
            "kpi_definitions",
            "default_currency",
            "notes",
        ]:
            if field_name in data and getattr(profile, field_name) != data[field_name]:
                setattr(profile, field_name, data[field_name])
                update_fields.append(field_name)

        if update_fields:
            update_fields.append("updated_at")
            profile.save(update_fields=update_fields)

        binding = bind_user_to_startup(
            user=user,
            organization=organization,
            google_connection=getattr(user, "google_connection", None),
            role=data.get("role", ""),
            is_default_for_gmail=bool(data.get("is_default_for_gmail", True)),
        )

        return Response(
            {
                "profile": _serialize_profile(profile),
                "binding": _serialize_binding(binding),
                "google_connected": bool(getattr(user, "google_connection", None)),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateRunView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = StartupUpdateRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = get_object_or_404(User, pk=data["user_id"])
        binding = get_default_binding_for_domain(user=user, domain=data["domain"])
        if binding is None:
            return Response(
                {"error": "No startup binding found for this user and domain."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        google_connection = binding.google_connection or getattr(user, "google_connection", None)
        if google_connection is None:
            return Response(
                {"error": "The bound user does not have a Gmail connection."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if binding.google_connection_id != google_connection.id:
            binding.google_connection = google_connection
            binding.save(update_fields=["google_connection", "updated_at"])

        organization = binding.organization
        _, profile = resolve_or_create_profile(domain=organization.domain)
        existing_run = ContentFactoryRun.objects.filter(
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=organization.domain,
            status__in=[
                ContentFactoryRunStatus.QUEUED,
                ContentFactoryRunStatus.RUNNING,
                ContentFactoryRunStatus.BLOCKED,
                ContentFactoryRunStatus.AWAITING_CONFIRMATION,
                ContentFactoryRunStatus.AWAITING_APPROVAL,
                ContentFactoryRunStatus.AWAITING_DELIVERY_MODE,
                ContentFactoryRunStatus.APPROVAL_REQUIRED,
            ],
        ).first()
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
            window_months=data.get("window_months", DEFAULT_BACKFILL_MONTHS),
        )
        GmailSyncCursor.objects.get_or_create(
            organization=organization,
            google_connection=google_connection,
        )

        transaction.on_commit(lambda: notify_valley_run_created(run.run_id))
        return Response(
            {
                "run": _serialize_run(run, request),
                "profile": _serialize_profile(profile),
                "binding": _serialize_binding(binding),
            },
            status=status.HTTP_200_OK if existing_run else status.HTTP_201_CREATED,
        )


class StartupUpdateIngestNextPageView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = StartupUpdateIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="gmail_backfill")
        cursor, _ = GmailSyncCursor.objects.get_or_create(
            organization=organization,
            google_connection=google_connection,
        )

        mode = data.get("mode", "backfill")

        run_request = run.run_request or {}
        start_raw = run_request.get("backfill_window_start")
        end_raw = run_request.get("backfill_window_end")
        if start_raw and end_raw:
            after_dt = datetime.fromisoformat(start_raw)
            before_dt = datetime.fromisoformat(end_raw)
            if timezone.is_naive(after_dt):
                after_dt = timezone.make_aware(after_dt, timezone=dt_timezone.utc)
            if timezone.is_naive(before_dt):
                before_dt = timezone.make_aware(before_dt, timezone=dt_timezone.utc)
        else:
            after_dt, before_dt = six_month_backfill_window()

        sync_result = None
        if mode == "incremental":
            start_history_id = data.get("start_history_id") or cursor.last_history_id
            if start_history_id:
                try:
                    sync_result = sync_history_metadata_page(
                        organization=organization,
                        connection=google_connection,
                        profile=profile,
                        start_history_id=start_history_id,
                        page_token=data.get("page_token"),
                        max_results=data.get("max_results", 250),
                    )
                except StaleHistoryCursorError:
                    cursor.last_history_id = ""
                    cursor.backfill_completed_at = None
                    cursor.save(update_fields=["last_history_id", "backfill_completed_at", "updated_at"])
                    after_dt = timezone.now() - timedelta(days=60)
                    before_dt = timezone.now()
                    sync_result = sync_message_metadata_page(
                        organization=organization,
                        connection=google_connection,
                        profile=profile,
                        after_dt=after_dt,
                        before_dt=before_dt,
                        page_token=data.get("page_token"),
                        max_results=data.get("max_results", 250),
                    )
                    sync_result["cursor_reset"] = True
            else:
                after_dt = timezone.now() - timedelta(days=60)
                before_dt = timezone.now()
                sync_result = sync_message_metadata_page(
                    organization=organization,
                    connection=google_connection,
                    profile=profile,
                    after_dt=after_dt,
                    before_dt=before_dt,
                    page_token=data.get("page_token"),
                    max_results=data.get("max_results", 250),
                )
                sync_result["mode"] = "backfill"
        else:
            sync_result = sync_message_metadata_page(
                organization=organization,
                connection=google_connection,
                profile=profile,
                after_dt=after_dt,
                before_dt=before_dt,
                page_token=data.get("page_token"),
                max_results=data.get("max_results", 250),
            )

        cursor.backfill_window_start = after_dt
        cursor.backfill_window_end = before_dt
        if sync_result["artifacts"]:
            cursor.last_message_internal_date = max(item.internal_date for item in sync_result["artifacts"])
            cursor.last_synced_internal_date = timezone.now()
            artifact_history_ids = [int(item.history_id) for item in sync_result["artifacts"] if str(item.history_id or "").isdigit()]
            if artifact_history_ids:
                cursor.last_history_id = str(max(artifact_history_ids))
        if sync_result.get("history_id"):
            cursor.last_history_id = sync_result["history_id"]
        if not sync_result.get("next_page_token"):
            cursor.backfill_completed_at = timezone.now()
        cursor.save()

        counts = {
            GmailRelevanceLabel.RELEVANT: 0,
            GmailRelevanceLabel.IRRELEVANT: 0,
            GmailRelevanceLabel.AMBIGUOUS: 0,
            GmailRelevanceLabel.PENDING: 0,
        }
        for artifact in sync_result["artifacts"]:
            counts[artifact.relevance_label] = counts.get(artifact.relevance_label, 0) + 1

        return Response(
            {
                "run": _serialize_run(run, request),
                "mode": sync_result.get("mode", mode),
                "ingested_count": len(sync_result["artifacts"]),
                "result_size_estimate": sync_result["result_size_estimate"],
                "next_page_token": sync_result["next_page_token"],
                "history_id": sync_result.get("history_id", ""),
                "cursor_reset": bool(sync_result.get("cursor_reset", False)),
                "relevance_counts": counts,
                "message_ids": [artifact.gmail_message_id for artifact in sync_result["artifacts"]],
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateHydrateThreadsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = StartupUpdateThreadHydrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="thread_hydration")

        thread_ids = set(data.get("thread_ids") or [])
        if data.get("message_ids"):
            for artifact in GmailMessageArtifact.objects.filter(
                organization=organization,
                gmail_message_id__in=data["message_ids"],
            ):
                thread_ids.add(artifact.gmail_thread_id)

        if not thread_ids:
            return Response(
                {"error": "At least one thread_id or message_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hydrated = []
        attachment_count = 0
        for thread_id in sorted(thread_ids):
            thread_artifact = hydrate_thread_artifact(
                organization=organization,
                connection=google_connection,
                thread_id=thread_id,
                profile=profile,
                fetch_attachments=True,
            )
            attachment_count += len(thread_artifact.attachment_ids or [])
            hydrated.append(thread_artifact.gmail_thread_id)

        return Response(
            {
                "run": _serialize_run(run, request),
                "hydrated_thread_ids": hydrated,
                "hydrated_count": len(hydrated),
                "attachment_count": attachment_count,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateHydrationCandidatesView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="thread_hydration")

        hydrated_by_thread = {
            item["gmail_thread_id"]: item["hydration_status"]
            for item in GmailThreadArtifact.objects.filter(organization=organization).values("gmail_thread_id", "hydration_status")
        }

        seen_thread_ids = set()
        candidates = []
        queryset = GmailMessageArtifact.objects.filter(
            organization=organization,
            relevance_label__in=[GmailRelevanceLabel.RELEVANT, GmailRelevanceLabel.AMBIGUOUS],
            needs_thread_context=True,
        ).order_by("-internal_date")

        for artifact in queryset:
            if artifact.gmail_thread_id in seen_thread_ids:
                continue
            thread_status = hydrated_by_thread.get(artifact.gmail_thread_id)
            if thread_status == ArtifactProcessingStatus.HYDRATED:
                continue
            seen_thread_ids.add(artifact.gmail_thread_id)
            candidates.append(
                {
                    "gmail_thread_id": artifact.gmail_thread_id,
                    "source_message_ids": [artifact.gmail_message_id],
                    "latest_message_internal_date": artifact.internal_date.isoformat(),
                }
            )
            if len(candidates) >= limit:
                break

        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(candidates),
                "threads": candidates,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateClassificationBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="relevance_classification")

        queryset = GmailMessageArtifact.objects.filter(
            organization=organization,
            relevance_label__in=[GmailRelevanceLabel.AMBIGUOUS, GmailRelevanceLabel.PENDING],
        ).order_by("-heuristic_score", "-internal_date")[:limit]

        payload = []
        for artifact in queryset:
            payload.append(
                {
                    "gmail_message_id": artifact.gmail_message_id,
                    "gmail_thread_id": artifact.gmail_thread_id,
                    "internal_date": artifact.internal_date.isoformat(),
                    "subject": artifact.subject,
                    "from_address": artifact.from_address,
                    "to_addresses": artifact.to_addresses or [],
                    "cc_addresses": artifact.cc_addresses or [],
                    "snippet": artifact.snippet,
                    "cleaned_text": artifact.cleaned_text,
                    "attachment_manifest": artifact.attachment_manifest or [],
                    "heuristic_score": artifact.heuristic_score,
                    "heuristic_reasons": artifact.heuristic_reasons or [],
                }
            )

        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(payload),
                "messages": payload,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateClassificationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = ClassificationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="relevance_classification")

        updated = 0
        for item in serializer.validated_data["results"]:
            artifact = get_object_or_404(
                GmailMessageArtifact,
                organization=organization,
                gmail_message_id=item["gmail_message_id"],
            )
            artifact.relevance_label = item["relevance_label"]
            artifact.relevance_score = item.get("relevance_score", 0.0)
            artifact.relevance_reason = item.get("relevance_reason", "")
            artifact.needs_thread_context = bool(item.get("needs_thread_context", False))
            artifact.classified_at = timezone.now()
            artifact.save(
                update_fields=[
                    "relevance_label",
                    "relevance_score",
                    "relevance_reason",
                    "needs_thread_context",
                    "classified_at",
                    "updated_at",
                ]
            )
            updated += 1

        return Response(
            {
                "run": _serialize_run(run, request),
                "updated_count": updated,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateExtractionBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="event_extraction")

        queryset = GmailThreadArtifact.objects.filter(
            organization=organization,
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status__in=[ArtifactProcessingStatus.PENDING, ArtifactProcessingStatus.HYDRATED],
        ).order_by("-latest_message_internal_date", "-updated_at")[:limit]

        bundles = []
        for thread_artifact in queryset:
            attachments = list(
                GmailAttachmentArtifact.objects.filter(thread_artifact=thread_artifact).order_by("id")
            )
            bundles.append(
                {
                    "gmail_thread_id": thread_artifact.gmail_thread_id,
                    "source_message_ids": thread_artifact.source_message_ids or [],
                    "source_message_count": thread_artifact.source_message_count,
                    "cleaned_text": thread_artifact.cleaned_text,
                    "participant_summary": thread_artifact.participant_summary or {},
                    "message_payloads": thread_artifact.message_payloads or [],
                    "attachments": [_serialize_attachment(attachment) for attachment in attachments],
                }
            )

        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(bundles),
                "threads": bundles,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateExtractionResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = ExtractionResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="event_extraction")

        event_count = 0
        metric_count = 0
        attachment_count = 0
        for item in serializer.validated_data["results"]:
            thread_artifact = get_object_or_404(
                GmailThreadArtifact,
                organization=organization,
                gmail_thread_id=item["gmail_thread_id"],
            )
            thread_artifact.extraction_status = item.get(
                "extraction_status",
                ArtifactProcessingStatus.PROCESSED,
            )
            thread_artifact.extracted_at = timezone.now()
            thread_artifact.save(update_fields=["extraction_status", "extracted_at", "updated_at"])

            for attachment_update in item.get("attachment_updates", []):
                attachment = get_object_or_404(
                    GmailAttachmentArtifact,
                    organization=organization,
                    id=attachment_update["id"],
                )
                attachment.extracted_text = attachment_update.get("extracted_text", "")
                attachment.extraction_status = attachment_update.get(
                    "extraction_status",
                    ArtifactProcessingStatus.PROCESSED,
                )
                attachment.parse_notes = attachment_update.get("parse_notes", "")
                attachment.extracted_at = timezone.now()
                attachment.save(
                    update_fields=[
                        "extracted_text",
                        "extraction_status",
                        "parse_notes",
                        "extracted_at",
                        "updated_at",
                    ]
                )
                attachment_count += 1

            for event_data in item.get("events", []):
                StartupEvent.objects.update_or_create(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                    defaults={
                        "run": run,
                        "event_type": event_data["event_type"],
                        "title": event_data["title"],
                        "summary": event_data.get("summary", ""),
                        "event_date": event_data.get("event_date"),
                        "month_bucket": event_data["month_bucket"],
                        "date_precision": event_data.get("date_precision"),
                        "sentiment": event_data.get("sentiment", ""),
                        "investor_importance": event_data.get("investor_importance", 3),
                        "quantitative_facts": event_data.get("quantitative_facts", []),
                        "evidence_message_ids": event_data.get("evidence_message_ids", []),
                        "evidence_attachment_ids": event_data.get("evidence_attachment_ids", []),
                        "source_thread_ids": event_data.get("source_thread_ids", []),
                        "confidence": event_data.get("confidence", 0.0),
                        "status": event_data.get("status", "open"),
                        "needs_review": bool(event_data.get("needs_review", False)),
                        "merge_notes": event_data.get("merge_notes", ""),
                    },
                )
                event_count += 1

            for metric_data in item.get("metrics", []):
                StartupMetricObservation.objects.update_or_create(
                    organization=organization,
                    source_thread=thread_artifact,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                    defaults={
                        "run": run,
                        "metric_name": metric_data["metric_name"],
                        "value_number": metric_data.get("value_number"),
                        "unit": metric_data.get("unit", ""),
                        "observed_at": metric_data.get("observed_at"),
                        "confidence": metric_data.get("confidence", 0.0),
                        "evidence_message_ids": metric_data.get("evidence_message_ids", []),
                        "evidence_attachment_ids": metric_data.get("evidence_attachment_ids", []),
                        "summary": metric_data.get("summary", ""),
                    },
                )
                metric_count += 1

        return Response(
            {
                "run": _serialize_run(run, request),
                "event_count": event_count,
                "metric_count": metric_count,
                "attachment_count": attachment_count,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateTimelineView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="timeline_merge")
        return Response(
            {
                "run": _serialize_run(run, request),
                "timeline": build_timeline_payload(organization=organization),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateDraftResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = DraftResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="draft_generation")

        saved = []
        for item in serializer.validated_data["drafts"]:
            draft = upsert_monthly_update_draft(
                organization=organization,
                month=item["month"],
                run=run,
                structured_memo=item["structured_memo"],
                model_name=item.get("model_name", ""),
                status=item.get("status"),
                groundedness_status=item.get("groundedness_status"),
                evidence_event_ids=item.get("evidence_event_ids", []),
                evidence_metric_ids=item.get("evidence_metric_ids", []),
                carry_forward_event_ids=item.get("carry_forward_event_ids", []),
                groundedness_notes=item.get("groundedness_notes", ""),
            )
            saved.append(_serialize_draft(draft))

        return Response(
            {
                "run": _serialize_run(run, request),
                "drafts": saved,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateDraftListView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = normalize_domain(request.query_params.get("domain") or "")
        if not domain:
            return Response(
                {"error": "domain query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = get_object_or_404(Organization, domain=domain)
        drafts = organization.monthly_update_drafts.order_by("-month", "-updated_at")
        return Response(
            {
                "domain": organization.domain,
                "drafts": [_serialize_draft(draft) for draft in drafts],
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateDraftDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, draft_id: int):
        draft = get_object_or_404(MonthlyUpdateDraft.objects.select_related("organization"), pk=draft_id)
        events = list(
            StartupEvent.objects.filter(
                organization=draft.organization,
                id__in=list(draft.evidence_event_ids or []),
            ).order_by("month_bucket", "-investor_importance", "title")
        )
        metrics = list(
            StartupMetricObservation.objects.filter(
                organization=draft.organization,
                id__in=list(draft.evidence_metric_ids or []),
            ).order_by("period_month", "metric_key")
        )
        return Response(
            {
                "draft": _serialize_draft(draft),
                "events": [_serialize_event(event) for event in events],
                "metrics": [_serialize_metric(metric) for metric in metrics],
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateOpenRunsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        raw_limit = request.query_params.get("limit") or 100
        try:
            limit = max(1, min(int(raw_limit), 200))
        except (TypeError, ValueError):
            limit = 100

        runs = list(
            ContentFactoryRun.objects.filter(
                workflow=STARTUP_UPDATE_WORKFLOW,
                status__in=list(OPEN_RUN_STATUSES),
            ).order_by("-updated_at")[:limit]
        )
        return Response(
            {
                "count": len(runs),
                "runs": [_serialize_open_run(run) for run in runs],
            },
            status=status.HTTP_200_OK,
        )
