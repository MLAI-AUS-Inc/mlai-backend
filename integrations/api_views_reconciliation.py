from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from roo.permissions import is_points_admin
from organizations.models import Organization
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryStepStatus,
)
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnection,
    ExternalServiceProvider,
    HumanitixPayout,
    ReconciliationMapping,
    ReconciliationDecision,
    ReconciliationPartyIdentity,
    ReconciliationProfile,
    ReconciliationRule,
    ReconciliationSuggestion,
    StripePayoutReconciliation,
    XeroStatementLineSnapshot,
    XeroStatementScan,
    XeroStatementSuggestion,
)
from integrations.services.humanitix_payouts import (
    HumanitixPayoutImportError,
    build_humanitix_xero_correction_batch,
    build_humanitix_xero_preview,
    import_payout_csv,
    post_humanitix_xero_bank_transaction,
    serialize_humanitix_payout,
)
from integrations.services.reconciliation import (
    ReconciliationReportService,
    StripeAPIError,
    StripeConfigurationError,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
    XeroPostingError,
    build_xero_correction_batch,
    build_xero_preview,
    persist_report,
    post_xero_bank_transaction,
    resolve_xero_connection,
    serialize_mapping,
    serialize_payout,
    serialize_profile,
    xero_has_bank_transaction_scope,
)
from integrations.services.xero_scopes import (
    xero_has_attachments_scope,
    xero_has_invoice_write_scope,
    xero_has_payment_write_scope,
)
from integrations.services.reconciliation_context import (
    approve_reconciliation_suggestion,
    build_reconciliation_enrichment_context,
    save_reconciliation_suggestions,
    serialize_suggestion,
)
from integrations.services.xero_statement_reconciliation import (
    import_xero_statement_lines,
    merchant_key,
    prepare_verified_rule_suggestions,
    save_statement_suggestions,
    serialize_statement_line,
    serialize_statement_suggestion,
)
from integrations.services.xero_statement_posting import (
    build_statement_posting_preview,
    execute_statement_posting,
)
from integrations.services.xero_bill_intake import (
    attach_reconciliation_document,
    build_reconciliation_bill_preview,
    create_reconciliation_bill,
)
from integrations.services.reconciliation_rules import (
    latest_admin_reconciliation_decision,
    record_reconciliation_decision,
    serialize_reconciliation_decision,
    serialize_reconciliation_rule,
    validate_description_template,
)
from integrations.services.reconciliation_outcomes import (
    build_reconciliation_outcome_summary,
    get_learning_candidate,
)
from integrations.services.reconciliation_knowledge import (
    build_reconciliation_knowledge_export,
)


MAX_WINDOW_DAYS = 92
DEFAULT_WINDOW_DAYS = 30


def _admin_or_response(request, *, from_body: bool = False):
    values = request.data if from_body else request.query_params
    slack_user_id = str(values.get("slack_user_id") or "").strip()
    if not slack_user_id:
        return None, Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
    if not is_points_admin(slack_user_id):
        return None, Response(
            {"error": "Only Points Admins can manage payout reconciliation"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return slack_user_id, None


def _organization_or_response(request, *, from_body: bool = False):
    from django.conf import settings

    values = request.data if from_body else request.query_params
    domain = str(values.get("domain") or getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au")).strip().lower()
    organization = Organization.objects.filter(domain__iexact=domain).first()
    if organization is None:
        return None, Response({"error": f"Unknown organisation domain: {domain}"}, status=status.HTTP_404_NOT_FOUND)
    return organization, None


RECONCILIATION_AGENT_WORKFLOW = "xero_reconciliation_agent"
RECONCILIATION_AGENT_STEP_ORDER = ["reconciliation_enrichment"]


def _stable_reconciliation_hash(payload) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _latest_statement_scan(organization):
    return XeroStatementScan.objects.filter(
        organization=organization,
    ).order_by("-started_at", "-id").first()


def _latest_monthly_context_run(organization):
    return ContentFactoryRun.objects.filter(
        organization=organization,
        workflow="startup_monthly_update",
        status=ContentFactoryRunStatus.COMPLETED,
    ).order_by("-updated_at", "-id").first()


def _statement_scan_freshness(scan) -> tuple[bool, int]:
    max_age_minutes = int(
        getattr(settings, "XERO_STATEMENT_SCAN_MAX_AGE_MINUTES", 30)
    )
    fresh = bool(
        scan
        and scan.completed_at
        and scan.completed_at
        >= datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    )
    return fresh, max_age_minutes


def _reconciliation_request_fingerprint(
    *,
    organization,
    scan,
    monthly_run,
    instruction: str,
    requested_line_ids: list[str],
) -> str:
    rule_revisions = list(
        ReconciliationRule.objects.filter(
            organization=organization,
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
        )
        .order_by("id")
        .values_list(
            "id",
            "scope",
            "statement_line_id",
            "bank_narration_key",
            "direction",
            "effective_from",
            "effective_to",
            "proposed_action",
            "contact_name",
            "account_code",
            "account_name",
            "tax_type",
            "description_template",
            "event_source_id",
            "event_tracking_option_name",
            "project_source_id",
            "project_tracking_option_name",
            "priority",
        )
    )
    bill_revisions = list(
        ExternalFinancialRecord.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
        )
        .exclude(status__in=["DELETED", "VOIDED", "PAID"])
        .order_by("external_record_id", "id")
        .values_list(
            "external_record_id",
            "status",
            "amount",
            "currency",
            "direction",
            "transaction_date",
            "merchant_name",
            "class_name",
        )
    )
    profile = (
        ReconciliationProfile.objects.filter(organization=organization)
        .select_related("xero_connection")
        .first()
    )
    return _stable_reconciliation_hash({
        "organization_id": organization.id,
        "statement_scan_id": scan.id,
        "base_monthly_run_id": monthly_run.run_id if monthly_run else "",
        "instruction": " ".join(str(instruction or "").split()),
        "requested_statement_line_ids": sorted(requested_line_ids),
        "verified_rule_revisions": rule_revisions,
        "outstanding_xero_bill_revisions": bill_revisions,
        "profile_revision": (
            {
                **serialize_profile(profile),
                "xero_connection_status": (
                    profile.xero_connection.status
                    if profile.xero_connection_id
                    else ""
                ),
            }
            if profile
            else None
        ),
    })


def _agent_run_start_response(run, *, idempotent: bool) -> dict:
    result = run.result if isinstance(run.result, dict) else {}
    summary = result.get("deterministic_reconciliation") or {}
    request_payload = run.run_request if isinstance(run.run_request, dict) else {}
    valley_meta = result.get("_valley_meta") or {}
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "status": run.status,
        "current_step": run.current_step,
        "statement_scan_id": request_payload.get("statement_scan_id"),
        "base_monthly_run_id": request_payload.get("base_monthly_run_id") or "",
        "request_fingerprint": request_payload.get("request_fingerprint") or "",
        "dry_run": True,
        **summary,
        "retry_available": bool(
            run.resume_available
            or valley_meta.get("dispatch_status") == "failed"
        ),
        "valley_dispatched": False if idempotent else (
            valley_meta.get("dispatch_status") == "queued"
        ),
        "idempotent": idempotent,
    }


def _reconciliation_run_or_response(*, organization, run_id: str):
    if not run_id:
        return None, Response({"error": "run_id is required"}, status=status.HTTP_400_BAD_REQUEST)
    run = (
        ContentFactoryRun.objects.filter(
            run_id=run_id,
            workflow__in=["startup_monthly_update", RECONCILIATION_AGENT_WORKFLOW],
        )
        .filter(Q(organization=organization) | Q(organization__isnull=True, domain__iexact=organization.domain))
        .first()
    )
    if run is None:
        return None, Response(
            {"error": "Reconciliation run does not belong to this organisation"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return run, None


class ReconciliationAdminView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def context(self, request, *, from_body: bool = False):
        slack_user_id, response = _admin_or_response(request, from_body=from_body)
        if response:
            return None, None, response
        organization, response = _organization_or_response(request, from_body=from_body)
        return slack_user_id, organization, response


class ReconciliationKnowledgeExportView(ReconciliationAdminView):
    """Admin-only, read-only and sanitized agent knowledge snapshot."""

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        return Response(build_reconciliation_knowledge_export(organization=organization))


class ReconciliationReportView(APIView):
    """Roo-only endpoint: Luma->Stripe reconciliation report for Points Admins.

    Payout-driven: returns each Stripe payout (= one bank deposit) with the
    ticket charges behind it, a Cowork markdown brief, and an optional xlsx
    audit workbook. Read-only against Stripe. Points-Admin gated (contains PII).
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        slack_user_id = str(request.query_params.get("slack_user_id") or "").strip()
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        if not is_points_admin(slack_user_id):
            return Response(
                {"error": "Only Points Admins (admin, committee, or portfolio_lead) can run reconciliation reports"},
                status=status.HTTP_403_FORBIDDEN,
            )

        window, error_response = self._resolve_window(request.query_params)
        if error_response:
            return error_response
        since, until = window

        include_workbook = self._parse_bool(request.query_params.get("include_workbook"), default=True)

        service = ReconciliationReportService()
        try:
            report = service.build_report(since=since, until=until, include_workbook=include_workbook)
        except StripeConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except StripeAPIError as exc:
            if exc.status_code == 429:
                return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(report)

    def post(self, request):
        """Run a bounded backfill and persist its payout ledger (never posts Xero)."""
        slack_user_id, error = _admin_or_response(request, from_body=True)
        if error:
            return error
        organization, error = _organization_or_response(request, from_body=True)
        if error:
            return error
        window, error = self._resolve_window(request.data)
        if error:
            return error
        try:
            report = ReconciliationReportService().build_report(
                since=window[0], until=window[1], include_workbook=False
            )
            profile = ReconciliationProfile.objects.filter(organization=organization).first()
            account_id = profile.stripe_account_id if profile else ""
            records = persist_report(
                organization=organization,
                report=report,
                stripe_account_id=account_id,
            )
        except StripeConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except StripeAPIError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                "payout_count": len(records),
                "payouts": [serialize_payout(record) for record in records],
                "requested_by": slack_user_id,
            },
            status=status.HTTP_200_OK,
        )

    # ---- window parsing --------------------------------------------------

    def _resolve_window(self, params):
        """Return ((since, until), None) or (None, error_response).

        Precedence: explicit since/until (YYYY-MM-DD) override the rolling
        `days` window. `until` defaults to now; `since` defaults to
        until - days. Window is capped at MAX_WINDOW_DAYS.
        """
        now = datetime.now(timezone.utc)

        # Validate `days` unconditionally so a malformed value is a clear 400 even
        # when since/until take precedence over it.
        days, err = self._parse_days(params.get("days"))
        if err:
            return None, err

        until, err = self._parse_date_end(params.get("until"), default=now)
        if err:
            return None, err
        since, err = self._parse_date_start(params.get("since"), default=None)
        if err:
            return None, err

        if since is None:
            since = until - timedelta(days=days)

        if since >= until:
            return None, Response(
                {"error": "since must be before until"}, status=status.HTTP_400_BAD_REQUEST
            )
        # Compare on calendar days: an explicit end date is inflated to end-of-day,
        # so a raw timedelta would spuriously reject an exactly-MAX_WINDOW_DAYS span.
        if (until.date() - since.date()).days > MAX_WINDOW_DAYS:
            return None, Response(
                {"error": f"window too large; max {MAX_WINDOW_DAYS} days"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return (since, until), None

    @staticmethod
    def _parse_days(raw_value):
        if raw_value in (None, ""):
            return DEFAULT_WINDOW_DAYS, None
        try:
            days = int(raw_value)
        except (TypeError, ValueError):
            return None, Response({"error": "days must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        if days < 1:
            return None, Response({"error": "days must be at least 1"}, status=status.HTTP_400_BAD_REQUEST)
        return min(days, MAX_WINDOW_DAYS), None

    @staticmethod
    def _parse_date_start(raw_value, *, default):
        if raw_value in (None, ""):
            return default, None
        try:
            d = datetime.fromisoformat(str(raw_value).strip()).date()
        except ValueError:
            return None, Response({"error": "since must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        return datetime.combine(d, time.min, tzinfo=timezone.utc), None

    @staticmethod
    def _parse_date_end(raw_value, *, default):
        if raw_value in (None, ""):
            return default, None
        try:
            d = datetime.fromisoformat(str(raw_value).strip()).date()
        except ValueError:
            return None, Response({"error": "until must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        # inclusive end-of-day for the given date
        return datetime.combine(d, time.max, tzinfo=timezone.utc), None

    @staticmethod
    def _parse_bool(raw_value, *, default: bool) -> bool:
        if raw_value is None:
            return default
        normalized = str(raw_value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default


class ReconciliationProfileView(ReconciliationAdminView):
    EDITABLE_FIELDS = {
        "stripe_account_id",
        "xero_bank_account_id",
        "xero_bank_account_name",
        "xero_contact_id",
        "xero_contact_name",
        "humanitix_contact_id",
        "humanitix_contact_name",
        "revenue_account_code",
        "fee_account_code",
        "refund_account_code",
        "revenue_tax_type",
        "fee_tax_type",
        "refund_tax_type",
        "line_amount_types",
        "event_tracking_category_id",
        "event_tracking_category_name",
        "project_tracking_category_id",
        "project_tracking_category_name",
        "standalone_fee_project_option_id",
        "standalone_fee_project_option_name",
        "enabled",
    }

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        profile, _ = ReconciliationProfile.objects.get_or_create(organization=organization)
        if profile.xero_connection_id is None:
            connection = resolve_xero_connection(organization)
            if connection:
                profile.xero_connection = connection
                profile.save(update_fields=["xero_connection", "updated_at"])
        return Response({"profile": serialize_profile(profile)})

    def put(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        profile, _ = ReconciliationProfile.objects.get_or_create(organization=organization)
        connection_id = request.data.get("xero_connection_id")
        if connection_id not in (None, ""):
            try:
                connection_id = int(connection_id)
            except (TypeError, ValueError):
                return Response({"error": "xero_connection_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
            connection = resolve_xero_connection(organization, connection_id)
            if connection is None:
                return Response({"error": "Xero connection does not belong to this organisation"}, status=status.HTTP_400_BAD_REQUEST)
            profile.xero_connection = connection
        for field in self.EDITABLE_FIELDS:
            if field in request.data:
                setattr(profile, field, request.data[field])
        if profile.line_amount_types not in {"Inclusive", "Exclusive", "NoTax"}:
            return Response({"error": "line_amount_types must be Inclusive, Exclusive, or NoTax"}, status=status.HTTP_400_BAD_REQUEST)
        profile.save()
        return Response({"profile": serialize_profile(profile)})


class ReconciliationMappingView(ReconciliationAdminView):
    EDITABLE_FIELDS = {
        "source_label",
        "accounting_treatment",
        "event_tracking_option_id",
        "event_tracking_option_name",
        "project_tracking_option_id",
        "project_tracking_option_name",
        "project_source_type",
        "project_source_id",
        "reconciliation_note",
        "account_code",
        "tax_type",
        "active",
    }

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        mappings = ReconciliationMapping.objects.filter(organization=organization).order_by("source_type", "source_label", "source_id")
        return Response({"mappings": [serialize_mapping(mapping) for mapping in mappings]})

    def put(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        items = request.data.get("mappings")
        if not isinstance(items, list):
            items = [request.data]
        saved = []
        valid_types = {choice[0] for choice in ReconciliationMapping.SOURCE_CHOICES}
        for item in items:
            if not isinstance(item, dict):
                return Response({"error": "Each mapping must be an object"}, status=status.HTTP_400_BAD_REQUEST)
            source_type = str(item.get("source_type") or "").strip()
            source_id = str(item.get("source_id") or "").strip()
            if source_type not in valid_types or not source_id:
                return Response({"error": "Each mapping needs a valid source_type and source_id"}, status=status.HTTP_400_BAD_REQUEST)
            defaults = {field: item[field] for field in self.EDITABLE_FIELDS if field in item}
            treatment = str(defaults.get("accounting_treatment") or "").strip()
            if treatment and treatment not in {choice[0] for choice in ReconciliationMapping.TREATMENT_CHOICES}:
                return Response({"error": "accounting_treatment must be revenue or clearing"}, status=status.HTTP_400_BAD_REQUEST)
            mapping, _ = ReconciliationMapping.objects.update_or_create(
                organization=organization,
                source_type=source_type,
                source_id=source_id,
                defaults=defaults,
            )
            saved.append(mapping)
        return Response({"mappings": [serialize_mapping(mapping) for mapping in saved]})


class ReconciliationEnrichmentContextView(APIView):
    """Machine-to-machine contract used by Valley after timeline merge."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        organization, error = _organization_or_response(request)
        if error:
            return error
        run_id = str(request.query_params.get("run_id") or "").strip()
        run, error = _reconciliation_run_or_response(organization=organization, run_id=run_id)
        if error:
            return error
        context = build_reconciliation_enrichment_context(organization=organization, run_id=run_id)
        if run.workflow == RECONCILIATION_AGENT_WORKFLOW:
            selected_line_ids = {
                str(item or "").strip()
                for item in (run.run_request or {}).get("statement_line_ids") or []
                if str(item or "").strip()
            }
            if selected_line_ids:
                context["statement_candidates"] = [
                    item for item in context.get("statement_candidates") or []
                    if str(item.get("statement_line_id") or "") in selected_line_ids
                ]
            context["agent_instruction"] = str((run.run_request or {}).get("instruction") or "")
            context["base_monthly_run_id"] = str((run.run_request or {}).get("base_monthly_run_id") or "")
        return Response(context)

    def post(self, request):
        organization, error = _organization_or_response(request, from_body=True)
        if error:
            return error
        run_id = str(request.data.get("run_id") or "").strip()
        run, error = _reconciliation_run_or_response(organization=organization, run_id=run_id)
        if error:
            return error
        suggestions = request.data.get("suggestions", [])
        statement_suggestions = request.data.get("statement_suggestions", [])
        if not isinstance(suggestions, list) or not isinstance(statement_suggestions, list):
            return Response(
                {"error": "suggestions and statement_suggestions must be lists"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            saved = save_reconciliation_suggestions(
                organization=organization,
                run_id=run_id,
                suggestions=suggestions,
                model_name=str(request.data.get("model_name") or ""),
            )
            saved_statements = save_statement_suggestions(
                organization=organization,
                run_id=run_id,
                suggestions=statement_suggestions,
                model_name=str(request.data.get("model_name") or ""),
            )
        except (TypeError, ValueError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        automatic_postings = []
        automatic_posting_allowed = bool(
            getattr(settings, "XERO_STATEMENT_AUTO_POST_ENABLED", False)
            and run.workflow != RECONCILIATION_AGENT_WORKFLOW
            and not bool((run.run_request or {}).get("dry_run", False))
        )
        if automatic_posting_allowed:
            for suggestion in saved_statements:
                try:
                    posting = execute_statement_posting(
                        suggestion,
                        requested_by_slack_id=f"monthly-update:{run_id}"[:100],
                        automatic=True,
                    )
                    automatic_postings.append({
                        "suggestion_id": suggestion.id,
                        "posting_id": posting.id,
                        "status": posting.status,
                    })
                except ReconciliationValidationError as exc:
                    automatic_postings.append({
                        "suggestion_id": suggestion.id,
                        "status": "not_ready",
                        "errors": exc.errors,
                    })
                except XeroPostingError as exc:
                    automatic_postings.append({
                        "suggestion_id": suggestion.id,
                        "status": "failed",
                        "errors": [str(exc)],
                    })
        return Response({
            "suggestion_count": len(saved),
            "suggestions": [serialize_suggestion(item) for item in saved],
            "statement_suggestion_count": len(saved_statements),
            "statement_suggestions": [serialize_statement_suggestion(item) for item in saved_statements],
            "automatic_posting_enabled": bool(
                automatic_posting_allowed
            ),
            "automatic_postings": automatic_postings,
        })


class ReconciliationStatementLineListView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        queryset = XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            active=True,
        ).order_by("transaction_date", "statement_line_id")
        return Response({"statement_lines": [serialize_statement_line(line) for line in queryset]})


def _serialize_party_identity(identity: ReconciliationPartyIdentity) -> dict:
    return {
        "id": identity.id,
        "bank_narration_key": identity.bank_narration_key,
        "direction": identity.direction,
        "canonical_name": identity.canonical_name,
        "xero_contact_id": identity.xero_contact_id,
        "xero_contact_name": identity.xero_contact_name,
        "linear_user_id": identity.linear_user_id,
        "linear_name": identity.linear_name,
        "linear_email": identity.linear_email,
        "status": identity.status,
        "confidence": identity.confidence,
        "verified_by_slack_id": identity.verified_by_slack_id,
        "verified_at": identity.verified_at.isoformat() if identity.verified_at else None,
        "active": identity.active,
        "notes": identity.notes,
    }


class ReconciliationPartyIdentityView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        identities = ReconciliationPartyIdentity.objects.filter(
            organization=organization,
        ).order_by("bank_narration_key", "direction")
        return Response({"identities": [_serialize_party_identity(item) for item in identities]})

    def put(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        statement_line_id = str(request.data.get("statement_line_id") or "").strip()
        line = None
        if statement_line_id:
            line = XeroStatementLineSnapshot.objects.filter(
                organization=organization,
                statement_line_id=statement_line_id,
            ).first()
            if line is None:
                return Response({"error": "Statement line was not found."}, status=status.HTTP_404_NOT_FOUND)
        narration_key = merchant_key(
            line.narration if line else request.data.get("bank_narration_key") or request.data.get("narration")
        )
        direction = str(line.direction if line else request.data.get("direction") or "").strip().lower()
        if not narration_key:
            return Response({"error": "A statement line or bank narration key is required."}, status=status.HTTP_400_BAD_REQUEST)
        if direction not in {"", XeroStatementLineSnapshot.DIRECTION_DEBIT, XeroStatementLineSnapshot.DIRECTION_CREDIT}:
            return Response({"error": "direction must be debit or credit"}, status=status.HTTP_400_BAD_REQUEST)
        identity_status = str(request.data.get("status") or ReconciliationPartyIdentity.STATUS_VERIFIED).strip().lower()
        if identity_status not in {
            ReconciliationPartyIdentity.STATUS_VERIFIED,
            ReconciliationPartyIdentity.STATUS_REVOKED,
        }:
            return Response({"error": "status must be verified or revoked"}, status=status.HTTP_400_BAD_REQUEST)
        linear_user_id = str(request.data.get("linear_user_id") or "").strip()
        linear_name = str(request.data.get("linear_name") or "").strip()
        linear_email = str(request.data.get("linear_email") or "").strip()
        if linear_user_id:
            from startup_updates.models import LinearProjectMemberArtifact

            member = LinearProjectMemberArtifact.objects.filter(
                organization=organization,
                linear_user_id=linear_user_id,
                active=True,
            ).order_by("-synced_at", "-id").first()
            if member is None:
                return Response({"error": "Linear user is not an active project member."}, status=status.HTTP_400_BAD_REQUEST)
            linear_name = linear_name or member.name
            linear_email = linear_email or member.email
        canonical_name = str(
            request.data.get("canonical_name") or linear_name or request.data.get("xero_contact_name") or ""
        ).strip()
        if identity_status == ReconciliationPartyIdentity.STATUS_VERIFIED and not canonical_name:
            return Response({"error": "canonical_name is required for a verified identity"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            identity_confidence = max(0.0, min(float(request.data.get("confidence", 1.0)), 1.0))
        except (TypeError, ValueError):
            return Response({"error": "confidence must be a number"}, status=status.HTTP_400_BAD_REQUEST)
        identity, _created = ReconciliationPartyIdentity.objects.update_or_create(
            organization=organization,
            bank_narration_key=narration_key,
            direction=direction,
            defaults={
                "canonical_name": canonical_name or narration_key,
                "xero_contact_id": str(request.data.get("xero_contact_id") or "").strip()[:255],
                "xero_contact_name": str(request.data.get("xero_contact_name") or canonical_name or "").strip()[:255],
                "linear_user_id": linear_user_id[:100],
                "linear_name": linear_name[:255],
                "linear_email": linear_email[:255],
                "status": identity_status,
                "confidence": identity_confidence,
                "verified_by_slack_id": slack_user_id if identity_status == ReconciliationPartyIdentity.STATUS_VERIFIED else "",
                "verified_at": datetime.now(timezone.utc) if identity_status == ReconciliationPartyIdentity.STATUS_VERIFIED else None,
                "active": identity_status == ReconciliationPartyIdentity.STATUS_VERIFIED,
                "notes": str(request.data.get("notes") or "")[:4000],
            },
        )
        return Response({"identity": _serialize_party_identity(identity)})


def _rule_date(value, field_name: str):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _save_reconciliation_rule(*, organization, slack_user_id: str, payload, rule=None):
    from startup_updates.models import (
        LinearProjectArtifact,
        LinearProjectSelection,
        LumaEventSelection,
    )

    scope = str(payload.get("scope") or getattr(rule, "scope", ReconciliationRule.SCOPE_MERCHANT)).strip()
    if scope not in {ReconciliationRule.SCOPE_MERCHANT, ReconciliationRule.SCOPE_STATEMENT_LINE}:
        raise ValueError("scope must be merchant or statement_line")
    statement_line_id = str(
        payload.get("statement_line_id")
        or (rule.statement_line.statement_line_id if rule and rule.statement_line_id else "")
    ).strip()
    statement_line = None
    if statement_line_id:
        statement_line = XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            statement_line_id=statement_line_id,
        ).first()
        if statement_line is None:
            raise ValueError("Statement line was not found")
    if scope == ReconciliationRule.SCOPE_STATEMENT_LINE and statement_line is None:
        raise ValueError("statement_line_id is required for a statement-line rule")

    narration_value = payload.get("bank_narration_key") or payload.get("narration")
    bank_narration_key = merchant_key(
        narration_value
        if narration_value is not None
        else (statement_line.narration if statement_line else getattr(rule, "bank_narration_key", ""))
    )
    direction = str(
        payload.get("direction")
        or (statement_line.direction if statement_line else getattr(rule, "direction", ""))
    ).strip().lower()
    if scope == ReconciliationRule.SCOPE_MERCHANT:
        if not bank_narration_key:
            raise ValueError("bank_narration_key or narration is required for a merchant rule")
        if direction not in {
            XeroStatementLineSnapshot.DIRECTION_DEBIT,
            XeroStatementLineSnapshot.DIRECTION_CREDIT,
        }:
            raise ValueError("direction must be debit or credit for a merchant rule")

    effective_from = _rule_date(
        payload.get("effective_from", getattr(rule, "effective_from", None)),
        "effective_from",
    )
    effective_to = _rule_date(
        payload.get("effective_to", getattr(rule, "effective_to", None)),
        "effective_to",
    )
    if effective_from and effective_to and effective_from > effective_to:
        raise ValueError("effective_from must be on or before effective_to")
    if scope == ReconciliationRule.SCOPE_STATEMENT_LINE and statement_line:
        effective_from = effective_from or statement_line.transaction_date
        effective_to = effective_to or statement_line.transaction_date

    action = str(
        payload.get("proposed_action")
        or getattr(rule, "proposed_action", ReconciliationRule.ACTION_CREATE_BANK_TRANSACTION)
    ).strip()
    if action != ReconciliationRule.ACTION_CREATE_BANK_TRANSACTION:
        raise ValueError("Verified rules currently support create_bank_transaction only")

    event_source_id = str(payload.get("event_source_id", getattr(rule, "event_source_id", "")) or "").strip()
    event_name = ""
    if event_source_id:
        event = LumaEventSelection.objects.filter(
            organization=organization,
            event_id=event_source_id,
        ).first()
        if event is None:
            raise ValueError("event_source_id is not a known Luma event")
        event_name = event.event_name
    project_source_id = str(payload.get("project_source_id", getattr(rule, "project_source_id", "")) or "").strip()
    project_name = ""
    if project_source_id:
        project = LinearProjectArtifact.objects.filter(
            organization=organization,
            linear_project_id=project_source_id,
        ).first() or LinearProjectSelection.objects.filter(
            organization=organization,
            linear_project_id=project_source_id,
        ).first()
        if project is None:
            raise ValueError("project_source_id is not a known Linear project")
        project_name = str(getattr(project, "name", "") or getattr(project, "project_name", ""))

    def value(name, default=""):
        return str(payload.get(name, getattr(rule, name, default)) or "").strip()

    description_template = validate_description_template(
        payload.get("description_template", getattr(rule, "description_template", ""))
    )
    required = {
        "name": value("name"),
        "contact_name": value("contact_name"),
        "account_code": value("account_code"),
        "account_name": value("account_name"),
        "tax_type": value("tax_type"),
    }
    missing = [name for name, item in required.items() if not item]
    if missing:
        raise ValueError("Verified rule fields are required: " + ", ".join(missing))
    try:
        priority = int(payload.get("priority", getattr(rule, "priority", 100)))
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer") from exc
    if priority < 0 or priority > 10000:
        raise ValueError("priority must be between 0 and 10000")

    requested_status = str(payload.get("status") or "").strip().lower()
    if requested_status and requested_status not in {
        ReconciliationRule.STATUS_PROPOSED,
        ReconciliationRule.STATUS_VERIFIED,
        ReconciliationRule.STATUS_REVOKED,
    }:
        raise ValueError("status must be proposed, verified or revoked")
    if requested_status in {ReconciliationRule.STATUS_VERIFIED, ReconciliationRule.STATUS_REVOKED} and payload.get("confirm") is not True:
        raise ValueError("confirm must be true to verify or revoke a reconciliation rule")
    if not requested_status:
        requested_status = (
            ReconciliationRule.STATUS_VERIFIED
            if payload.get("confirm") is True
            else (rule.status if rule else ReconciliationRule.STATUS_PROPOSED)
        )
    if rule and rule.status == ReconciliationRule.STATUS_VERIFIED and requested_status == ReconciliationRule.STATUS_VERIFIED and payload.get("confirm") is not True:
        raise ValueError("confirm must be true to update a verified reconciliation rule")

    evidence = payload.get("evidence", getattr(rule, "evidence", []))
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    now = datetime.now(timezone.utc)
    values = {
        **required,
        "scope": scope,
        "statement_line": statement_line if scope == ReconciliationRule.SCOPE_STATEMENT_LINE else None,
        "bank_narration_key": bank_narration_key,
        "direction": direction,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "proposed_action": action,
        "description_template": description_template,
        "event_source_id": event_source_id,
        "event_tracking_option_name": event_name,
        "project_source_id": project_source_id,
        "project_tracking_option_name": project_name,
        "priority": priority,
        "status": requested_status,
        "active": requested_status == ReconciliationRule.STATUS_VERIFIED,
        "evidence": evidence[:20],
        "notes": value("notes")[:4000],
        "verified_by_slack_id": slack_user_id if requested_status == ReconciliationRule.STATUS_VERIFIED else "",
        "verified_at": now if requested_status == ReconciliationRule.STATUS_VERIFIED else None,
    }
    if rule is None:
        return ReconciliationRule.objects.create(organization=organization, **values)
    for name, item in values.items():
        setattr(rule, name, item)
    rule.save()
    return rule


class ReconciliationRuleListView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        rules = ReconciliationRule.objects.filter(organization=organization).select_related(
            "statement_line"
        ).order_by("-active", "-priority", "-updated_at", "-id")
        return Response({"rules": [serialize_reconciliation_rule(rule) for rule in rules]})

    def post(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        try:
            rule = _save_reconciliation_rule(
                organization=organization,
                slack_user_id=slack_user_id,
                payload=request.data,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"rule": serialize_reconciliation_rule(rule)}, status=status.HTTP_201_CREATED)


class ReconciliationRuleDetailView(ReconciliationAdminView):
    def put(self, request, rule_id: int):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        rule = ReconciliationRule.objects.filter(
            organization=organization,
            id=rule_id,
        ).select_related("statement_line").first()
        if rule is None:
            return Response({"error": "Reconciliation rule was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            rule = _save_reconciliation_rule(
                organization=organization,
                slack_user_id=slack_user_id,
                payload=request.data,
                rule=rule,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"rule": serialize_reconciliation_rule(rule)})


class ReconciliationDecisionListView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 100), 250))
        except (TypeError, ValueError):
            return Response({"error": "limit must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        decisions = ReconciliationDecision.objects.filter(
            organization=organization,
        ).select_related("statement_line", "suggestion", "rule")
        statement_line_id = str(request.query_params.get("statement_line_id") or "").strip()
        run_id = str(request.query_params.get("run_id") or "").strip()
        if statement_line_id:
            decisions = decisions.filter(statement_line__statement_line_id=statement_line_id)
        if run_id:
            decisions = decisions.filter(run_id=run_id)
        decisions = decisions.order_by("-created_at", "-id")[:limit]
        return Response({
            "decisions": [serialize_reconciliation_decision(decision) for decision in decisions]
        })


class ReconciliationOutcomeView(ReconciliationAdminView):
    """Report confirmed human outcomes and read-only rule candidates."""

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            return Response({"error": "limit must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        limit = max(1, min(limit, 200))
        return Response(build_reconciliation_outcome_summary(
            organization=organization,
            limit=limit,
        ))


class ReconciliationLearningCandidateView(ReconciliationAdminView):
    """Preview or explicitly decide one revalidated learning candidate."""

    def get(self, request, candidate_id: str):
        _, organization, error = self.context(request)
        if error:
            return error
        candidate = get_learning_candidate(
            organization=organization,
            candidate_id=candidate_id,
        )
        if candidate is None:
            return Response(
                {"error": "Learning candidate was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({
            "candidate": candidate,
            "automatic_rule_creation": False,
        })

    def post(self, request, candidate_id: str):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        decision = str(request.data.get("decision") or "").strip().lower()
        if decision not in {"promote", "reject"}:
            return Response(
                {"error": "decision must be promote or reject"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if request.data.get("confirm") is not True:
            return Response(
                {"error": "confirm must be true to decide a learning candidate"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        expected_version = str(request.data.get("candidate_version") or "").strip()
        if not expected_version:
            return Response(
                {"error": "candidate_version from the reviewed preview is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reason = str(request.data.get("reason") or "").strip()[:2000]
        if decision == "reject" and not reason:
            return Response(
                {"error": "A reason is required when rejecting a learning candidate"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        candidate = get_learning_candidate(
            organization=organization,
            candidate_id=candidate_id,
        )
        if candidate is None:
            return Response(
                {"error": "Learning candidate was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        line_ids = candidate["example_statement_line_ids"]
        with transaction.atomic():
            locked_lines = list(
                XeroStatementLineSnapshot.objects.select_for_update()
                .filter(
                    organization=organization,
                    statement_line_id__in=line_ids,
                )
                .order_by("transaction_date", "id")
            )
            candidate = get_learning_candidate(
                organization=organization,
                candidate_id=candidate_id,
            )
            if candidate is None:
                return Response(
                    {"error": "Learning candidate changed and is no longer available."},
                    status=status.HTTP_409_CONFLICT,
                )
            if candidate["candidate_version"] != expected_version:
                return Response(
                    {
                        "error": "Learning candidate changed after preview; review the latest version.",
                        "candidate": candidate,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            if not locked_lines:
                return Response(
                    {"error": "Learning candidate has no confirmed source lines."},
                    status=status.HTTP_409_CONFLICT,
                )
            representative_line = locked_lines[0]

            if decision == "reject":
                if candidate["review_status"] in {"promoted", "already_covered"}:
                    return Response(
                        {"error": "This candidate is already represented by a verified rule; revoke that rule instead."},
                        status=status.HTTP_409_CONFLICT,
                    )
                audit = record_reconciliation_decision(
                    statement_line=representative_line,
                    decision_type=ReconciliationDecision.TYPE_LEARNING_RULE_REJECTED,
                    actor_type=ReconciliationDecision.ACTOR_ADMIN,
                    actor_id=slack_user_id,
                    outcome={
                        "candidate_id": candidate["candidate_id"],
                        "candidate_version": candidate["candidate_version"],
                        "reason": reason,
                        "example_statement_line_ids": line_ids,
                    },
                    evidence=[
                        {
                            "source_provider": "xero_ui",
                            "source_record_id": line.statement_line_id,
                            "summary": "Human-confirmed reconciliation outcome.",
                        }
                        for line in locked_lines[:20]
                    ],
                    discriminator=f"learning-reject:{candidate['candidate_id']}:{candidate['candidate_version']}:{reason}",
                )
                refreshed = get_learning_candidate(
                    organization=organization,
                    candidate_id=candidate_id,
                )
                return Response({
                    "decision": "rejected",
                    "decision_id": audit.id,
                    "candidate": refreshed,
                    "automatic_rule_creation": False,
                })

            if candidate["review_status"] == "promoted" and candidate.get("rule_id"):
                existing_rule = ReconciliationRule.objects.filter(
                    organization=organization,
                    id=candidate["rule_id"],
                ).first()
                if existing_rule:
                    return Response({
                        "decision": "promoted",
                        "idempotent": True,
                        "rule": serialize_reconciliation_rule(existing_rule),
                        "candidate": candidate,
                    })
            if not candidate["eligible_for_promotion"]:
                return Response(
                    {
                        "error": "Learning candidate is not safe to promote.",
                        "blocking_reasons": candidate["blocking_reasons"],
                        "candidate": candidate,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            suggested = candidate["suggested_rule"]
            evidence = [
                {
                    "source_provider": "xero_ui",
                    "source_record_id": line.statement_line_id,
                    "summary": "Human-confirmed reconciliation outcome.",
                }
                for line in locked_lines[:20]
            ]
            rule = _save_reconciliation_rule(
                organization=organization,
                slack_user_id=slack_user_id,
                payload={
                    "scope": ReconciliationRule.SCOPE_MERCHANT,
                    "name": str(
                        request.data.get("name")
                        or f"Learned: {suggested['contact_name'] or candidate['merchant_key']}"
                    )[:255],
                    "bank_narration_key": candidate["merchant_key"],
                    "direction": candidate["direction"],
                    "effective_from": suggested["effective_from"],
                    "effective_to": suggested["effective_to"],
                    "proposed_action": ReconciliationRule.ACTION_CREATE_BANK_TRANSACTION,
                    "contact_name": suggested["contact_name"],
                    "account_code": suggested["account_code"],
                    "account_name": suggested["account_name"],
                    "tax_type": suggested["tax_type"],
                    "description_template": suggested["description_template"],
                    "event_source_id": suggested["event_source_id"],
                    "project_source_id": suggested["project_source_id"],
                    "priority": 100,
                    "status": ReconciliationRule.STATUS_VERIFIED,
                    "confirm": True,
                    "evidence": evidence,
                    "notes": (
                        f"Promoted from learning candidate {candidate['candidate_id']} "
                        f"after {candidate['confirmed_example_count']} confirmed reconciliations."
                    ),
                },
            )
            audit = record_reconciliation_decision(
                statement_line=representative_line,
                rule=rule,
                decision_type=ReconciliationDecision.TYPE_LEARNING_RULE_PROMOTED,
                actor_type=ReconciliationDecision.ACTOR_ADMIN,
                actor_id=slack_user_id,
                outcome={
                    "candidate_id": candidate["candidate_id"],
                    "candidate_version": candidate["candidate_version"],
                    "rule_id": rule.id,
                    "example_statement_line_ids": line_ids,
                },
                evidence=evidence,
                discriminator=f"learning-promote:{candidate['candidate_id']}:{candidate['candidate_version']}",
            )
            refreshed = get_learning_candidate(
                organization=organization,
                candidate_id=candidate_id,
            )
            return Response({
                "decision": "promoted",
                "decision_id": audit.id,
                "idempotent": False,
                "rule": serialize_reconciliation_rule(rule),
                "candidate": refreshed,
                "automatic_rule_creation": False,
            }, status=status.HTTP_201_CREATED)


class ReconciliationReadinessView(ReconciliationAdminView):
    """Report whether the current queue can be analysed and later written safely."""

    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error

        blockers: list[str] = []
        warnings: list[str] = []
        latest_scan = _latest_statement_scan(organization)
        scan_fresh, max_age_minutes = _statement_scan_freshness(latest_scan)
        candidate_count = 0
        if latest_scan is None:
            blockers.append("Import the current complete Xero bank-feed queue.")
        else:
            if latest_scan.status != XeroStatementScan.STATUS_COMPLETE:
                blockers.append("The latest Xero statement scan is incomplete.")
            if (
                latest_scan.expected_count is not None
                and latest_scan.expected_count != latest_scan.observed_count
            ):
                if "The latest Xero statement scan is incomplete." not in blockers:
                    blockers.append("The latest Xero statement scan is incomplete.")
            if not scan_fresh:
                blockers.append(
                    f"The latest Xero statement scan is older than {max_age_minutes} minutes."
                )
            candidate_count = sum(
                1
                for line in XeroStatementLineSnapshot.objects.filter(
                    organization=organization,
                    active=True,
                    last_scan=latest_scan,
                )
                if line.is_reconciliation_candidate
            )
            if candidate_count == 0:
                blockers.append("The latest Xero statement scan has no unreconciled candidates.")

        monthly_run = _latest_monthly_context_run(organization)
        if monthly_run is None:
            warnings.append(
                "Run a monthly update first so unresolved lines can use Gmail, Slack, Linear, Luma and Stripe context."
            )

        profile = (
            ReconciliationProfile.objects.filter(organization=organization)
            .select_related("xero_connection")
            .first()
        )
        connection = profile.xero_connection if profile else None
        connection_active = bool(
            connection
            and connection.provider == ExternalServiceProvider.XERO
            and connection.status != "disconnected"
        )
        bank_account_configured = bool(profile and profile.xero_bank_account_id)
        bank_transaction_scope = bool(
            connection_active and xero_has_bank_transaction_scope(connection.scopes)
        )
        payment_write_scope = bool(
            connection_active and xero_has_payment_write_scope(connection.scopes)
        )
        invoice_write_scope = bool(
            connection_active and xero_has_invoice_write_scope(connection.scopes)
        )
        attachments_scope = bool(
            connection_active and xero_has_attachments_scope(connection.scopes)
        )
        event_tracking_configured = bool(
            profile and profile.event_tracking_category_id
        )
        project_tracking_configured = bool(
            profile and profile.project_tracking_category_id
        )
        profile_enabled = bool(profile and profile.enabled)

        if not profile_enabled:
            warnings.append("Configure and enable the organisation's reconciliation profile.")
        if not connection_active:
            warnings.append("Reconnect the organisation's Xero account.")
        if not bank_account_configured:
            warnings.append("Configure the Xero bank account used for reconciliation.")
        if not bank_transaction_scope:
            warnings.append(
                "Reconnect Xero with accounting.banktransactions before creating Spend/Receive Money transactions."
            )
        if not payment_write_scope:
            warnings.append(
                "Reconnect Xero with accounting.payments before paying existing bills."
            )
        if not invoice_write_scope:
            warnings.append(
                "Reconnect Xero with accounting.invoices before creating draft bills."
            )
        if not attachments_scope:
            warnings.append(
                "Reconnect Xero with accounting.attachments before attaching source documents."
            )
        if not event_tracking_configured:
            warnings.append("Configure the Xero Event Name tracking category.")
        if not project_tracking_configured:
            warnings.append("Configure the Xero Project Name tracking category.")

        active_rule_count = ReconciliationRule.objects.filter(
            organization=organization,
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
        ).count()
        ready_to_execute_bank_transactions = bool(
            profile_enabled
            and connection_active
            and bank_account_configured
            and bank_transaction_scope
        )
        ready_to_execute_bill_payments = bool(
            profile_enabled
            and connection_active
            and bank_account_configured
            and payment_write_scope
        )
        ready_to_start = not blockers
        if not ready_to_start:
            recommended_next_action = blockers[0]
        elif monthly_run is None:
            recommended_next_action = "Run a monthly update, then start Xero reconciliation."
        else:
            recommended_next_action = "Start Xero reconciliation in preview-only mode."

        return Response({
            "domain": organization.domain,
            "ready_to_start": ready_to_start,
            "ready_to_execute_bank_transactions": ready_to_execute_bank_transactions,
            "ready_to_execute_bill_payments": ready_to_execute_bill_payments,
            "tracking_ready": bool(
                event_tracking_configured and project_tracking_configured
            ),
            "latest_statement_scan": (
                {
                    "id": latest_scan.id,
                    "status": latest_scan.status,
                    "completed_at": latest_scan.completed_at,
                    "fresh": scan_fresh,
                    "max_age_minutes": max_age_minutes,
                    "expected_count": latest_scan.expected_count,
                    "observed_count": latest_scan.observed_count,
                    "candidate_count": candidate_count,
                }
                if latest_scan
                else None
            ),
            "monthly_context": (
                {
                    "run_id": monthly_run.run_id,
                    "status": monthly_run.status,
                    "updated_at": monthly_run.updated_at,
                }
                if monthly_run
                else None
            ),
            "xero": {
                "profile_configured": bool(profile),
                "profile_enabled": profile_enabled,
                "connection_active": connection_active,
                "connection_id": connection.id if connection else None,
                "bank_account_configured": bank_account_configured,
                "bank_transaction_scope": bank_transaction_scope,
                "payment_write_scope": payment_write_scope,
                "invoice_write_scope": invoice_write_scope,
                "attachments_scope": attachments_scope,
                "event_tracking_configured": event_tracking_configured,
                "project_tracking_configured": project_tracking_configured,
            },
            "active_verified_rule_count": active_rule_count,
            "blockers": blockers,
            "warnings": warnings,
            "recommended_next_action": recommended_next_action,
        })


class ReconciliationAgentRunView(ReconciliationAdminView):
    """Create a preview-only reconciliation reasoning run in Valley."""

    def post(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        latest_scan = _latest_statement_scan(organization)
        if latest_scan is None:
            return Response(
                {"error": "Import a complete Xero statement scan before starting the agent."},
                status=status.HTTP_409_CONFLICT,
            )
        if latest_scan.status != XeroStatementScan.STATUS_COMPLETE:
            return Response(
                {"error": "The latest Xero statement scan is incomplete."},
                status=status.HTTP_409_CONFLICT,
            )
        if (
            latest_scan.expected_count is not None
            and latest_scan.expected_count != latest_scan.observed_count
        ):
            return Response(
                {"error": "The latest Xero statement scan count is incomplete."},
                status=status.HTTP_409_CONFLICT,
            )
        scan_fresh, max_scan_age_minutes = _statement_scan_freshness(latest_scan)
        if not scan_fresh:
            return Response(
                {"error": f"The latest Xero statement scan is older than {max_scan_age_minutes} minutes."},
                status=status.HTTP_409_CONFLICT,
            )

        raw_line_ids = request.data.get("statement_line_ids") or []
        if not isinstance(raw_line_ids, list):
            return Response({"error": "statement_line_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
        statement_line_ids = list(dict.fromkeys(
            str(item or "").strip() for item in raw_line_ids if str(item or "").strip()
        ))
        current_scan_lines = XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            active=True,
            last_scan=latest_scan,
        )
        if statement_line_ids:
            valid_ids = set(current_scan_lines.filter(
                statement_line_id__in=statement_line_ids,
            ).values_list("statement_line_id", flat=True))
            unknown = [item for item in statement_line_ids if item not in valid_ids]
            if unknown:
                return Response(
                    {"error": "Some statement lines are not active for this organisation.", "statement_line_ids": unknown},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            statement_line_ids = [
                line.statement_line_id
                for line in current_scan_lines.order_by("transaction_date", "statement_line_id")
                if line.is_reconciliation_candidate
            ]

        base_monthly_run = _latest_monthly_context_run(organization)
        requested_line_ids = statement_line_ids
        instruction = str(
            request.data.get("instruction")
            or "Reconcile the current Xero statement queue."
        )[:4000]
        request_fingerprint = _reconciliation_request_fingerprint(
            organization=organization,
            scan=latest_scan,
            monthly_run=base_monthly_run,
            instruction=instruction,
            requested_line_ids=requested_line_ids,
        )
        run_id = f"xero-reconciliation-{request_fingerprint[:40]}"
        existing_run = ContentFactoryRun.objects.filter(
            organization=organization,
            workflow=RECONCILIATION_AGENT_WORKFLOW,
            run_id=run_id,
        ).first()
        if existing_run is not None:
            return Response(
                _agent_run_start_response(existing_run, idempotent=True),
                status=status.HTTP_200_OK,
            )

        try:
            with transaction.atomic():
                run = ContentFactoryRun.objects.create(
                    run_id=run_id,
                    workflow=RECONCILIATION_AGENT_WORKFLOW,
                    domain=organization.domain,
                    organization=organization,
                    slack_user_id=slack_user_id,
                    status=ContentFactoryRunStatus.QUEUED,
                    current_step=RECONCILIATION_AGENT_STEP_ORDER[0],
                    approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
                    step_order=RECONCILIATION_AGENT_STEP_ORDER,
                )
                step = ContentFactoryRunStep.objects.create(
                    run=run,
                    step_key=RECONCILIATION_AGENT_STEP_ORDER[0],
                    display_order=0,
                    required=True,
                )
                prepared = prepare_verified_rule_suggestions(
                    organization=organization,
                    run_id=run.run_id,
                    statement_line_ids=requested_line_ids,
                )
                agent_line_ids = prepared["unresolved_line_ids"]
                deterministic_summary = {
                    "requested_line_count": len(requested_line_ids),
                    "deterministic_suggestion_count": len(prepared["deterministic_line_ids"]),
                    "rule_conflict_count": len(prepared["conflict_line_ids"]),
                    "deferred_bill_count": len(prepared["deferred_bill_line_ids"]),
                    "agent_line_count": len(agent_line_ids),
                }
                run.run_request = {
                    "organization_id": organization.id,
                    "instruction": instruction,
                    "request_fingerprint": request_fingerprint,
                    "statement_line_ids": agent_line_ids,
                    "requested_statement_line_ids": requested_line_ids,
                    "deterministic_statement_line_ids": prepared["deterministic_line_ids"],
                    "rule_conflict_statement_line_ids": prepared["conflict_line_ids"],
                    "deferred_bill_statement_line_ids": prepared["deferred_bill_line_ids"],
                    "statement_scan_id": latest_scan.id,
                    "base_monthly_run_id": base_monthly_run.run_id if base_monthly_run else "",
                    "dry_run": True,
                    "input_sources": ["gmail", "slack", "linear", "luma", "stripe", "xero"],
                }
                run.result = {"deterministic_reconciliation": deterministic_summary}
                if not agent_line_ids:
                    completed_at = datetime.now(timezone.utc)
                    run.status = ContentFactoryRunStatus.COMPLETED
                    run.current_step = ""
                    step.status = ContentFactoryStepStatus.COMPLETED
                    step.completed_at = completed_at
                    step.message = "All selected lines were resolved by verified rules or rule-conflict checks."
                    step.save(update_fields=[
                        "status", "completed_at", "message",
                    ])
                run.save(update_fields=[
                    "run_request", "result", "status", "current_step", "updated_at",
                ])
        except IntegrityError:
            existing_run = ContentFactoryRun.objects.filter(
                organization=organization,
                workflow=RECONCILIATION_AGENT_WORKFLOW,
                run_id=run_id,
            ).first()
            if existing_run is None:
                raise
            return Response(
                _agent_run_start_response(existing_run, idempotent=True),
                status=status.HTTP_200_OK,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        valley_dispatched = False
        if not agent_line_ids:
            return Response({
                "run_id": run.run_id,
                "workflow": run.workflow,
                "status": run.status,
                "current_step": run.current_step,
                "statement_scan_id": latest_scan.id,
                "base_monthly_run_id": base_monthly_run.run_id if base_monthly_run else "",
                "dry_run": True,
                **deterministic_summary,
                "valley_dispatched": False,
                "retry_available": False,
                "request_fingerprint": request_fingerprint,
                "idempotent": False,
            }, status=status.HTTP_201_CREATED)

        from integrations.services.valley_harness import notify_valley_run_created
        from startup_updates.services import record_valley_dispatch_result

        dispatch_result = notify_valley_run_created(run.run_id)
        record_valley_dispatch_result(run, dispatch_result)
        if not dispatch_result:
            run.resume_available = True
            run.save(update_fields=["resume_available", "updated_at"])
            return Response(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "error": "valley_dispatch_failed",
                    "retryable": True,
                    "retry_available": True,
                    **deterministic_summary,
                    "valley_dispatched": False,
                    "request_fingerprint": request_fingerprint,
                    "idempotent": False,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        valley_dispatched = True
        return Response({
            "run_id": run.run_id,
            "workflow": run.workflow,
            "status": run.status,
            "current_step": run.current_step,
            "statement_scan_id": latest_scan.id,
            "base_monthly_run_id": base_monthly_run.run_id if base_monthly_run else "",
            "dry_run": True,
            **deterministic_summary,
            "valley_dispatched": valley_dispatched,
            "retry_available": False,
            "request_fingerprint": request_fingerprint,
            "idempotent": False,
        }, status=status.HTTP_201_CREATED)


class ReconciliationAgentRunDetailView(ReconciliationAdminView):
    def get(self, request, run_id: str):
        _, organization, error = self.context(request)
        if error:
            return error
        run = ContentFactoryRun.objects.filter(
            organization=organization,
            workflow=RECONCILIATION_AGENT_WORKFLOW,
            run_id=run_id,
        ).first()
        if run is None:
            return Response({"error": "Reconciliation agent run was not found."}, status=status.HTTP_404_NOT_FOUND)
        suggestions = XeroStatementSuggestion.objects.filter(
            organization=organization,
            run_id=run.run_id,
        ).select_related("statement_line").order_by("statement_line__transaction_date", "statement_line_id")
        valley_meta = (
            (run.result or {}).get("_valley_meta") or {}
            if isinstance(run.result, dict)
            else {}
        )
        return Response({
            "run_id": run.run_id,
            "workflow": run.workflow,
            "status": run.status,
            "current_step": run.current_step,
            "error": run.error,
            "retry_available": bool(
                run.resume_available
                or valley_meta.get("dispatch_status") == "failed"
            ),
            "valley_dispatch": {
                "status": valley_meta.get("dispatch_status") or "",
                "job_id": valley_meta.get("last_dispatch_job_id") or "",
                "failure_kind": valley_meta.get("last_dispatch_error_kind") or "",
                "error": valley_meta.get("last_dispatch_error") or "",
                "last_attempt_at": valley_meta.get("last_dispatch_attempt_at"),
            },
            "deterministic_reconciliation": (
                (run.result or {}).get("deterministic_reconciliation") or {}
            ),
            "suggestions": [serialize_statement_suggestion(item) for item in suggestions],
        })


def _reconciliation_run_retry_error(*, organization, run) -> str:
    scan_id = (run.run_request or {}).get("statement_scan_id")
    latest_scan = _latest_statement_scan(organization)
    if latest_scan is None or latest_scan.id != scan_id:
        return "The Xero statement queue changed after this run started. Start a new reconciliation run."
    if (
        latest_scan.expected_count is not None
        and latest_scan.expected_count != latest_scan.observed_count
    ):
        return "The run's Xero statement scan is incomplete. Import a fresh complete scan."
    max_scan_age_minutes = int(
        getattr(settings, "XERO_STATEMENT_SCAN_MAX_AGE_MINUTES", 30)
    )
    if (
        latest_scan.completed_at is None
        or latest_scan.completed_at
        < datetime.now(timezone.utc) - timedelta(minutes=max_scan_age_minutes)
    ):
        return f"The run's statement scan is older than {max_scan_age_minutes} minutes. Import a fresh scan."
    line_ids = {
        str(item or "").strip()
        for item in (run.run_request or {}).get("statement_line_ids") or []
        if str(item or "").strip()
    }
    active_ids = set(
        XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            active=True,
            last_scan=latest_scan,
            statement_line_id__in=line_ids,
        ).values_list("statement_line_id", flat=True)
    )
    if active_ids != line_ids:
        return "One or more statement lines changed after this run started. Start a new reconciliation run."
    return ""


class ReconciliationAgentRunRetryView(ReconciliationAdminView):
    """Explicitly retry only the Valley analysis step for a durable run."""

    def post(self, request, run_id: str):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response(
                {"error": "confirm must be true to retry reconciliation analysis"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        run = ContentFactoryRun.objects.filter(
            organization=organization,
            workflow=RECONCILIATION_AGENT_WORKFLOW,
            run_id=run_id,
        ).first()
        if run is None:
            return Response(
                {"error": "Reconciliation agent run was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if run.status == ContentFactoryRunStatus.COMPLETED:
            return Response({
                "run_id": run.run_id,
                "status": run.status,
                "current_step": run.current_step,
                "retry_available": False,
                "valley_dispatched": False,
                "idempotent": True,
                "message": "The reconciliation run is already complete.",
            })
        if run.status == ContentFactoryRunStatus.RUNNING:
            return Response(
                {"error": "The reconciliation agent is already running."},
                status=status.HTTP_409_CONFLICT,
            )
        if run.status not in {
            ContentFactoryRunStatus.QUEUED,
            ContentFactoryRunStatus.FAILED,
            ContentFactoryRunStatus.BLOCKED,
        }:
            return Response(
                {"error": f"Reconciliation agent run cannot be retried from {run.status}."},
                status=status.HTTP_409_CONFLICT,
            )

        valley_meta = (
            (run.result or {}).get("_valley_meta") or {}
            if isinstance(run.result, dict)
            else {}
        )
        if (
            run.status == ContentFactoryRunStatus.QUEUED
            and valley_meta.get("dispatch_status") == "queued"
        ):
            return Response({
                "run_id": run.run_id,
                "status": run.status,
                "current_step": run.current_step,
                "retry_available": False,
                "valley_dispatched": False,
                "idempotent": True,
                "message": "The reconciliation agent is already queued.",
            })

        retry_error = _reconciliation_run_retry_error(
            organization=organization,
            run=run,
        )
        if retry_error:
            return Response(
                {"error": retry_error},
                status=status.HTTP_409_CONFLICT,
            )

        if run.status in {
            ContentFactoryRunStatus.FAILED,
            ContentFactoryRunStatus.BLOCKED,
        }:
            with transaction.atomic():
                run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
                run.status = ContentFactoryRunStatus.QUEUED
                run.current_step = RECONCILIATION_AGENT_STEP_ORDER[0]
                run.error = ""
                run.resume_available = False
                run.save(update_fields=[
                    "status", "current_step", "error", "resume_available", "updated_at",
                ])
                ContentFactoryRunStep.objects.filter(
                    run=run,
                    step_key=RECONCILIATION_AGENT_STEP_ORDER[0],
                ).update(
                    status=ContentFactoryStepStatus.PENDING,
                    message="",
                    started_at=None,
                    completed_at=None,
                    error="",
                )

        from integrations.services.valley_harness import notify_valley_run_created
        from startup_updates.services import record_valley_dispatch_result

        dispatch_result = notify_valley_run_created(run.run_id)
        record_valley_dispatch_result(run, dispatch_result)
        if not dispatch_result:
            run.resume_available = True
            run.save(update_fields=["resume_available", "updated_at"])
            return Response(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "error": "valley_dispatch_failed",
                    "retryable": True,
                    "retry_available": True,
                    "valley_dispatched": False,
                    "idempotent": False,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        run.resume_available = False
        run.save(update_fields=["resume_available", "updated_at"])
        return Response({
            "run_id": run.run_id,
            "status": run.status,
            "current_step": run.current_step,
            "retry_available": False,
            "valley_dispatched": True,
            "idempotent": False,
            "message": "Reconciliation analysis was queued again against the original fresh statement scan.",
        })


def _agent_run_suggestions(*, organization, run_id: str):
    run = ContentFactoryRun.objects.filter(
        organization=organization,
        workflow=RECONCILIATION_AGENT_WORKFLOW,
        run_id=run_id,
    ).first()
    if run is None:
        return None, None
    suggestions = XeroStatementSuggestion.objects.filter(
        organization=organization,
        run_id=run.run_id,
    ).select_related("statement_line", "statement_line__last_scan").order_by(
        "statement_line__transaction_date", "statement_line_id"
    )
    return run, suggestions


def _fresh_statement_scan_error(suggestion: XeroStatementSuggestion) -> str:
    scan = suggestion.statement_line.last_scan
    if scan is None or scan.status != XeroStatementScan.STATUS_COMPLETE:
        return "Import a complete Xero statement scan before execution."
    if scan.expected_count is not None and scan.expected_count != scan.observed_count:
        return "The latest Xero statement scan count is incomplete."
    max_age_minutes = int(getattr(settings, "XERO_STATEMENT_SCAN_MAX_AGE_MINUTES", 30))
    if (
        scan.completed_at is None
        or scan.completed_at < datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    ):
        return f"The statement scan is older than {max_age_minutes} minutes. Import a fresh scan."
    return ""


def _approval_matches_preview(
    suggestion: XeroStatementSuggestion,
    approval: ReconciliationDecision | None,
    preview: dict,
) -> bool:
    if approval is None or approval.decision_type != ReconciliationDecision.TYPE_ADMIN_APPROVED:
        return False
    outcome = approval.outcome or {}
    return bool(
        preview.get("ready")
        and outcome.get("suggestion_source_hash") == suggestion.source_hash
        and outcome.get("statement_source_hash") == suggestion.statement_line.source_hash
        and outcome.get("payload_hash") == preview.get("payload_hash")
    )


class ReconciliationAgentRunPreviewView(ReconciliationAdminView):
    def get(self, request, run_id: str):
        _, organization, error = self.context(request)
        if error:
            return error
        run, suggestions = _agent_run_suggestions(organization=organization, run_id=run_id)
        if run is None:
            return Response({"error": "Reconciliation agent run was not found."}, status=status.HTTP_404_NOT_FOUND)
        results = []
        for suggestion in suggestions:
            try:
                preview = build_statement_posting_preview(suggestion)
            except ReconciliationValidationError as exc:
                preview = {"ready": False, "errors": exc.errors or [str(exc)]}
            serialized = serialize_statement_suggestion(suggestion)
            approval = latest_admin_reconciliation_decision(suggestion)
            if (
                approval is not None
                and approval.decision_type == ReconciliationDecision.TYPE_ADMIN_APPROVED
                and not _approval_matches_preview(suggestion, approval, preview)
            ):
                serialized["approval"] = {
                    **serialized.get("approval", {}),
                    "status": "stale",
                    "current": False,
                }
            elif serialized.get("approval", {}).get("status") == "approved":
                serialized["approval"]["current"] = True
            results.append({"suggestion": serialized, "preview": preview})
        return Response({
            "run_id": run.run_id,
            "run_status": run.status,
            "suggestion_count": len(results),
            "ready_count": sum(1 for item in results if item["preview"].get("ready")),
            "approved_count": sum(
                1
                for item in results
                if item["suggestion"].get("approval", {}).get("status") == "approved"
            ),
            "deterministic_reconciliation": (
                (run.result or {}).get("deterministic_reconciliation") or {}
            ),
            "routing_counts": {
                source: sum(
                    1
                    for item in results
                    if (item["suggestion"].get("routing") or {}).get("source") == source
                )
                for source in {
                    (item["suggestion"].get("routing") or {}).get("source")
                    for item in results
                    if (item["suggestion"].get("routing") or {}).get("source")
                }
            },
            "results": results,
        })


class ReconciliationAgentRunDecisionView(ReconciliationAdminView):
    """Record explicit admin approval or rejection for one completed agent run."""

    def post(self, request, run_id: str):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response(
                {"error": "confirm must be true to record reconciliation decisions"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        run, suggestions = _agent_run_suggestions(organization=organization, run_id=run_id)
        if run is None:
            return Response({"error": "Reconciliation agent run was not found."}, status=status.HTTP_404_NOT_FOUND)
        if run.status != ContentFactoryRunStatus.COMPLETED:
            return Response(
                {"error": f"Reconciliation agent run is {run.status}; wait for completion before approval."},
                status=status.HTTP_409_CONFLICT,
            )
        suggestion_by_id = {suggestion.id: suggestion for suggestion in suggestions}
        approve_all_ready = request.data.get("approve_all_ready") is True
        raw_decisions = request.data.get("decisions") or []
        if approve_all_ready and raw_decisions:
            return Response(
                {"error": "Use approve_all_ready or decisions, not both."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if approve_all_ready:
            decision_items = [
                {"suggestion_id": suggestion.id, "decision": "approve"}
                for suggestion in suggestion_by_id.values()
            ]
        elif isinstance(raw_decisions, list) and raw_decisions:
            decision_items = raw_decisions
        else:
            return Response(
                {"error": "Provide approve_all_ready=true or a non-empty decisions list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        decision_request_id = str(
            request.data.get("decision_request_id") or uuid.uuid4()
        ).strip()[:100]
        results = []
        for item in decision_items:
            if not isinstance(item, dict):
                results.append({"recorded": False, "error": "Decision must be an object."})
                continue
            try:
                suggestion_id = int(item.get("suggestion_id"))
            except (TypeError, ValueError):
                results.append({"recorded": False, "error": "suggestion_id must be an integer."})
                continue
            suggestion = suggestion_by_id.get(suggestion_id)
            if suggestion is None:
                results.append({
                    "suggestion_id": suggestion_id,
                    "recorded": False,
                    "error": "Suggestion does not belong to this run.",
                })
                continue
            decision_name = str(item.get("decision") or "").strip().lower()
            if decision_name not in {"approve", "reject"}:
                results.append({
                    "suggestion_id": suggestion_id,
                    "recorded": False,
                    "error": "decision must be approve or reject.",
                })
                continue
            if suggestion.status != XeroStatementSuggestion.STATUS_PROPOSED:
                results.append({
                    "suggestion_id": suggestion_id,
                    "recorded": False,
                    "error": f"Suggestion status is {suggestion.status}, not proposed.",
                })
                continue
            if decision_name == "reject":
                reason = str(item.get("reason") or request.data.get("reason") or "Admin rejected the suggestion.")[:2000]
                decision = record_reconciliation_decision(
                    statement_line=suggestion.statement_line,
                    suggestion=suggestion,
                    decision_type=ReconciliationDecision.TYPE_ADMIN_REJECTED,
                    run_id=run.run_id,
                    actor_type=ReconciliationDecision.ACTOR_ADMIN,
                    actor_id=slack_user_id,
                    outcome={
                        "reason": reason,
                        "suggestion_source_hash": suggestion.source_hash,
                    },
                    evidence=suggestion.evidence or [],
                    # A later approval must be able to supersede a rejection (and
                    # vice versa), while retries of the same request stay
                    # idempotent.
                    discriminator=f"{decision_request_id}:reject:{reason}",
                )
                results.append({
                    "suggestion_id": suggestion.id,
                    "recorded": True,
                    "decision": serialize_reconciliation_decision(decision),
                })
                continue
            try:
                preview = build_statement_posting_preview(suggestion)
            except ReconciliationValidationError as exc:
                preview = {"ready": False, "errors": exc.errors or [str(exc)]}
            if not preview.get("ready"):
                results.append({
                    "suggestion_id": suggestion.id,
                    "recorded": False,
                    "error": "Suggestion is not ready for approval.",
                    "errors": preview.get("errors") or [],
                })
                continue
            decision = record_reconciliation_decision(
                statement_line=suggestion.statement_line,
                suggestion=suggestion,
                decision_type=ReconciliationDecision.TYPE_ADMIN_APPROVED,
                run_id=run.run_id,
                actor_type=ReconciliationDecision.ACTOR_ADMIN,
                actor_id=slack_user_id,
                outcome={
                    "suggestion_source_hash": suggestion.source_hash,
                    "statement_source_hash": suggestion.statement_line.source_hash,
                    "payload_hash": preview.get("payload_hash") or "",
                    "operation": preview.get("operation"),
                },
                evidence=suggestion.evidence or [],
                discriminator=f"{decision_request_id}:approve:{preview.get('payload_hash') or ''}",
            )
            results.append({
                "suggestion_id": suggestion.id,
                "recorded": True,
                "decision": serialize_reconciliation_decision(decision),
            })
        return Response({
            "run_id": run.run_id,
            "decision_request_id": decision_request_id,
            "requested_count": len(decision_items),
            "recorded_count": sum(1 for item in results if item.get("recorded")),
            "results": results,
        })


class ReconciliationAgentRunExecuteView(ReconciliationAdminView):
    """Execute only suggestions whose latest run-scoped decision is approval."""

    def post(self, request, run_id: str):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response(
                {"error": "confirm must be true to write approved transactions to Xero"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        run, suggestions = _agent_run_suggestions(organization=organization, run_id=run_id)
        if run is None:
            return Response({"error": "Reconciliation agent run was not found."}, status=status.HTTP_404_NOT_FOUND)
        if run.status != ContentFactoryRunStatus.COMPLETED:
            return Response(
                {"error": f"Reconciliation agent run is {run.status}; wait for completion before execution."},
                status=status.HTTP_409_CONFLICT,
            )
        raw_ids = request.data.get("suggestion_ids") or []
        if not isinstance(raw_ids, list):
            return Response({"error": "suggestion_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
        selected_ids = set()
        for item in raw_ids:
            try:
                selected_ids.add(int(item))
            except (TypeError, ValueError):
                return Response({"error": "suggestion_ids must contain integers"}, status=status.HTTP_400_BAD_REQUEST)
        if selected_ids:
            suggestions = suggestions.filter(id__in=selected_ids)

        results = []
        approved_candidate_count = 0
        for suggestion in suggestions:
            approval = latest_admin_reconciliation_decision(suggestion)
            if approval is None or approval.decision_type != ReconciliationDecision.TYPE_ADMIN_APPROVED:
                results.append({
                    "suggestion_id": suggestion.id,
                    "executed": False,
                    "error": "The latest admin decision is not approval.",
                })
                continue
            approved_candidate_count += 1
            scan_error = _fresh_statement_scan_error(suggestion)
            if scan_error:
                results.append({
                    "suggestion_id": suggestion.id,
                    "executed": False,
                    "error": scan_error,
                })
                continue
            try:
                preview = build_statement_posting_preview(suggestion)
            except ReconciliationValidationError as exc:
                preview = {"ready": False, "errors": exc.errors or [str(exc)]}
            approval_outcome = approval.outcome or {}
            approval_is_current = _approval_matches_preview(suggestion, approval, preview)
            if not approval_is_current:
                errors = preview.get("errors") or []
                if preview.get("ready"):
                    errors = ["The approved Xero payload changed; approve the fresh preview before execution."]
                record_reconciliation_decision(
                    statement_line=suggestion.statement_line,
                    suggestion=suggestion,
                    decision_type=ReconciliationDecision.TYPE_EXECUTION_BLOCKED,
                    run_id=run.run_id,
                    actor_type=ReconciliationDecision.ACTOR_SYSTEM,
                    outcome={
                        "approval_decision_id": approval.id,
                        "approved_payload_hash": approval_outcome.get("payload_hash") or "",
                        "current_payload_hash": preview.get("payload_hash") or "",
                        "errors": errors,
                    },
                    evidence=suggestion.evidence or [],
                    discriminator=f"approval:{approval.id}",
                )
                results.append({
                    "suggestion_id": suggestion.id,
                    "executed": False,
                    "error": "Approval is stale or the suggestion is no longer ready.",
                    "errors": errors,
                })
                continue
            try:
                posting = execute_statement_posting(
                    suggestion,
                    requested_by_slack_id=slack_user_id,
                    automatic=False,
                )
                results.append({
                    "suggestion_id": suggestion.id,
                    "executed": True,
                    "posting_id": posting.id,
                    "status": posting.status,
                    "xero_bank_transaction_id": posting.xero_bank_transaction_id,
                    "xero_payment_id": posting.xero_payment_id,
                })
            except ReconciliationValidationError as exc:
                results.append({
                    "suggestion_id": suggestion.id,
                    "executed": False,
                    "error": str(exc),
                    "errors": exc.errors,
                })
            except XeroPostingError as exc:
                results.append({
                    "suggestion_id": suggestion.id,
                    "executed": False,
                    "error": str(exc),
                })
        unknown_ids = selected_ids - {item["suggestion_id"] for item in results}
        for suggestion_id in sorted(unknown_ids):
            results.append({
                "suggestion_id": suggestion_id,
                "executed": False,
                "error": "Suggestion does not belong to this run.",
            })
        return Response({
            "run_id": run.run_id,
            "approved_candidate_count": approved_candidate_count,
            "executed_count": sum(1 for item in results if item.get("executed")),
            "results": results,
            "human_reconciliation_required": True,
        })


class ReconciliationStatementScanView(ReconciliationAdminView):
    """Import one admin-observed Xero queue scan with completeness guards."""

    def post(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        bank_account_id = str(request.data.get("bank_account_id") or "").strip()
        lines = request.data.get("lines")
        complete = request.data.get("complete", True)
        if not isinstance(complete, bool):
            return Response({"error": "complete must be a boolean"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            saved = import_xero_statement_lines(
                organization=organization,
                bank_account_id=bank_account_id,
                lines=lines,
                currency=str(request.data.get("currency") or "AUD"),
                expected_count=request.data.get("expected_count"),
                complete_scan=complete,
                source="browser",
                requested_by=slack_user_id,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        scan = (
            saved[0].last_scan
            if saved
            else XeroStatementScan.objects.filter(
                organization=organization,
                bank_account_id=bank_account_id,
            ).order_by("-id").first()
        )
        return Response({
            "scan": {
                "id": scan.id,
                "status": scan.status,
                "bank_account_id": scan.bank_account_id,
                "expected_count": scan.expected_count,
                "observed_count": scan.observed_count,
                "confirmed_reconciled_count": scan.confirmed_postings.count(),
                "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
            } if scan else None,
            "statement_lines": [serialize_statement_line(line) for line in saved],
        }, status=status.HTTP_201_CREATED)


def _serialize_posting_reference(posting) -> dict:
    """The Xero identifiers a caller needs to follow up on an executed posting
    (e.g. attach the source document to the created transaction)."""

    return {
        "id": posting.id,
        "operation": posting.operation,
        "status": posting.status,
        "xero_bank_transaction_id": posting.xero_bank_transaction_id,
        "xero_payment_id": posting.xero_payment_id,
        "xero_bill_id": posting.xero_bill_id,
    }


class ReconciliationStatementSuggestionPreviewView(ReconciliationAdminView):
    def get(self, request, suggestion_id: int):
        _, organization, error = self.context(request)
        if error:
            return error
        suggestion = XeroStatementSuggestion.objects.filter(
            organization=organization,
            id=suggestion_id,
        ).select_related("statement_line").first()
        if suggestion is None:
            return Response({"error": "Statement suggestion was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            preview = build_statement_posting_preview(suggestion)
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        return Response({"suggestion": serialize_statement_suggestion(suggestion), "preview": preview})


class ReconciliationStatementSuggestionExecuteView(ReconciliationAdminView):
    def post(self, request, suggestion_id: int):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response({"error": "confirm must be true to write to Xero"}, status=status.HTTP_400_BAD_REQUEST)
        suggestion = XeroStatementSuggestion.objects.filter(
            organization=organization,
            id=suggestion_id,
        ).select_related("statement_line").first()
        if suggestion is None:
            return Response({"error": "Statement suggestion was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            posting = execute_statement_posting(
                suggestion,
                requested_by_slack_id=slack_user_id,
                automatic=request.data.get("automatic") is True,
            )
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        except XeroPostingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        suggestion.refresh_from_db()
        return Response({
            "suggestion": serialize_statement_suggestion(suggestion),
            "posting_id": posting.id,
            "posting": _serialize_posting_reference(posting),
        })


class ReconciliationStatementSafeBatchView(ReconciliationAdminView):
    """Preview or execute the latest safe suggestion for each active row."""

    def post(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        dry_run = request.data.get("dry_run", True) is not False
        if not dry_run and request.data.get("confirm") is not True:
            return Response({"error": "confirm must be true to write to Xero"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            max_count = max(1, min(int(request.data.get("max_count") or 50), 100))
        except (TypeError, ValueError):
            return Response({"error": "max_count must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        suggestions = XeroStatementSuggestion.objects.filter(
            organization=organization,
            status=XeroStatementSuggestion.STATUS_PROPOSED,
            statement_line__active=True,
        ).exclude(
            statement_line__ui_mode=XeroStatementLineSnapshot.UI_GREEN_MATCH,
        ).exclude(
            statement_line__ui_mode=XeroStatementLineSnapshot.UI_UNKNOWN,
            statement_line__ready_in_xero=True,
        ).select_related("statement_line").order_by(
            "statement_line_id", "-created_at"
        )
        latest = []
        seen_lines = set()
        for suggestion in suggestions:
            if suggestion.statement_line_id in seen_lines:
                continue
            seen_lines.add(suggestion.statement_line_id)
            latest.append(suggestion)
            if len(latest) >= max_count:
                break

        results = []
        for suggestion in latest:
            try:
                preview = build_statement_posting_preview(suggestion)
                result = {
                    "suggestion_id": suggestion.id,
                    "statement_line_id": suggestion.statement_line.statement_line_id,
                    "ready": preview["ready"],
                    "operation": preview["operation"],
                    "errors": preview["errors"],
                }
                if preview["ready"] and not dry_run:
                    posting = execute_statement_posting(
                        suggestion,
                        requested_by_slack_id=slack_user_id,
                        automatic=request.data.get("automatic") is True,
                    )
                    result.update({
                        "posted": True,
                        "posting_id": posting.id,
                        "status": posting.status,
                        "posting": _serialize_posting_reference(posting),
                    })
                results.append(result)
            except ReconciliationValidationError as exc:
                results.append({
                    "suggestion_id": suggestion.id,
                    "statement_line_id": suggestion.statement_line.statement_line_id,
                    "ready": False,
                    "errors": exc.errors,
                })
            except XeroPostingError as exc:
                results.append({
                    "suggestion_id": suggestion.id,
                    "statement_line_id": suggestion.statement_line.statement_line_id,
                    "ready": False,
                    "posted": False,
                    "errors": [str(exc)],
                })
        return Response({
            "dry_run": dry_run,
            "candidate_count": len(results),
            "ready_count": sum(1 for item in results if item.get("ready")),
            "posted_count": sum(1 for item in results if item.get("posted")),
            "results": results,
        })


class ReconciliationDraftBillView(ReconciliationAdminView):
    """Create an ACCPAY bill from an extracted supplier invoice so the bank
    statement line green-matches the bill on the reconcile screen."""

    def post(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        dry_run = request.data.get("dry_run") is True
        if not dry_run and request.data.get("confirm") is not True:
            return Response({"error": "confirm must be true to write to Xero"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            if dry_run:
                preview = build_reconciliation_bill_preview(organization, payload=request.data)
                return Response({"dry_run": True, **preview})
            result = create_reconciliation_bill(
                organization,
                payload=request.data,
                requested_by_slack_id=slack_user_id,
            )
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        except XeroPostingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            result,
            status=status.HTTP_201_CREATED if result.get("created") else status.HTTP_200_OK,
        )


class ReconciliationXeroAttachmentView(ReconciliationAdminView):
    """Attach the source document (invoice PDF) to a Xero invoice or bank
    transaction — the audit trail behind every agent-created transaction."""

    def post(self, request):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response({"error": "confirm must be true to write to Xero"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = attach_reconciliation_document(
                organization,
                payload=request.data,
                requested_by_slack_id=slack_user_id,
            )
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        except XeroPostingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            result,
            status=status.HTTP_201_CREATED if result.get("created") else status.HTTP_200_OK,
        )


class ReconciliationSuggestionDecisionView(ReconciliationAdminView):
    def post(self, request, suggestion_id: int):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        suggestion = ReconciliationSuggestion.objects.filter(
            organization=organization,
            id=suggestion_id,
        ).select_related("payout").first()
        if suggestion is None:
            return Response({"error": "Suggestion was not found"}, status=status.HTTP_404_NOT_FOUND)
        decision = str(request.data.get("decision") or "").strip().lower()
        if decision == "reject":
            if suggestion.status != ReconciliationSuggestion.STATUS_PROPOSED:
                return Response(
                    {"error": "Only a proposed reconciliation suggestion can be rejected."},
                    status=status.HTTP_409_CONFLICT,
                )
            suggestion.status = ReconciliationSuggestion.STATUS_REJECTED
            suggestion.reviewed_by_slack_id = slack_user_id
            suggestion.reviewed_at = datetime.now(timezone.utc)
            suggestion.save(update_fields=["status", "reviewed_by_slack_id", "reviewed_at", "updated_at"])
            return Response({"suggestion": serialize_suggestion(suggestion)})
        if decision != "approve":
            return Response({"error": "decision must be approve or reject"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            approved, mapping = approve_reconciliation_suggestion(
                suggestion,
                reviewed_by_slack_id=slack_user_id,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"suggestion": serialize_suggestion(approved), "mapping": serialize_mapping(mapping)})


class ReconciliationPayoutListView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        records = StripePayoutReconciliation.objects.filter(organization=organization).order_by("-arrival_date", "-id")[:250]
        return Response({"payouts": [serialize_payout(record) for record in records]})


class ReconciliationPayoutCorrectionPreviewView(ReconciliationAdminView):
    """Build a read-only Stripe/Luma-to-Xero correction pack.

    The preview fetches Xero's accounting transactions but never creates,
    edits, voids, unreconciles, or reconciles anything.
    """

    def post(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        try:
            max_count = max(1, min(int(request.data.get("max_count") or 250), 250))
        except (TypeError, ValueError):
            return Response(
                {"error": "max_count must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = StripePayoutReconciliation.objects.filter(
            organization=organization,
        ).order_by("arrival_date", "id")
        cashflow_period = {"since": None, "until": None}
        for field_name, lookup in (("since", "arrival_date__gte"), ("until", "arrival_date__lte")):
            raw_value = str(request.data.get(field_name) or "").strip()
            if not raw_value:
                continue
            try:
                parsed_value = datetime.fromisoformat(raw_value).date()
            except ValueError:
                return Response(
                    {"error": f"{field_name} must use YYYY-MM-DD"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cashflow_period[field_name] = parsed_value
            queryset = queryset.filter(**{lookup: parsed_value})

        records = list(queryset[:max_count])
        try:
            preview = build_xero_correction_batch(
                records,
                cashflow_period_start=cashflow_period["since"],
                cashflow_period_end=cashflow_period["until"],
            )
        except ReconciliationProfile.DoesNotExist:
            return Response(
                {"error": "Reconciliation profile is not configured."},
                status=status.HTTP_409_CONFLICT,
            )
        except ReconciliationValidationError as exc:
            return Response(
                {"error": str(exc), "errors": exc.errors},
                status=status.HTTP_409_CONFLICT,
            )
        except requests.RequestException:
            return Response(
                {"error": "Unable to read Xero bank transactions for the correction preview."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "dry_run": True,
                "xero_writes": False,
                **preview,
            }
        )


class HumanitixPayoutListView(ReconciliationAdminView):
    def get(self, request):
        _, organization, error = self.context(request)
        if error:
            return error
        records = (
            HumanitixPayout.objects.filter(organization=organization)
            .prefetch_related("lines")
            .order_by("-payout_date", "-id")[:500]
        )
        return Response(
            {
                "payouts": [
                    serialize_humanitix_payout(record)
                    for record in records
                ]
            }
        )


class HumanitixPayoutCorrectionPreviewView(ReconciliationAdminView):
    """Compare Humanitix payout previews with Xero without making Xero writes."""

    def post(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        try:
            max_count = max(1, min(int(request.data.get("max_count") or 500), 500))
        except (TypeError, ValueError):
            return Response(
                {"error": "max_count must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        records = list(
            HumanitixPayout.objects.filter(organization=organization)
            .prefetch_related("lines")
            .order_by("payout_date", "id")[:max_count]
        )
        try:
            preview = build_humanitix_xero_correction_batch(records)
        except ReconciliationProfile.DoesNotExist:
            return Response(
                {"error": "Reconciliation profile is not configured."},
                status=status.HTTP_409_CONFLICT,
            )
        except ReconciliationValidationError as exc:
            return Response(
                {"error": str(exc), "errors": exc.errors},
                status=status.HTTP_409_CONFLICT,
            )
        except requests.RequestException:
            return Response(
                {
                    "error": (
                        "Unable to read Xero bank transactions for the "
                        "Humanitix correction preview."
                    )
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "dry_run": True,
                "xero_writes": False,
                **preview,
            }
        )


class HumanitixPayoutImportView(ReconciliationAdminView):
    MAX_UPLOAD_BYTES = 10 * 1024 * 1024

    def post(self, request):
        _, organization, error = self.context(request, from_body=True)
        if error:
            return error
        connection = (
            ExternalServiceConnection.objects.filter(
                organization=organization,
                provider=ExternalServiceProvider.HUMANITIX,
            )
            .exclude(status="disconnected")
            .order_by("-updated_at", "-id")
            .first()
        )
        if connection is None:
            return Response(
                {"error": "Humanitix is not connected for this organisation."},
                status=status.HTTP_409_CONFLICT,
            )
        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size > self.MAX_UPLOAD_BYTES:
                return Response(
                    {"error": "Humanitix payout CSV must be 10 MB or smaller."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            raw_csv = upload.read()
        else:
            raw_csv = request.data.get("csv") or request.data.get("csv_content") or ""
            if len(raw_csv.encode("utf-8")) > self.MAX_UPLOAD_BYTES:
                return Response(
                    {"error": "Humanitix payout CSV must be 10 MB or smaller."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if not raw_csv:
            return Response(
                {"error": "Upload the Humanitix global Payouts CSV."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payouts = import_payout_csv(
                organization=organization,
                connection=connection,
                source=raw_csv,
            )
            previews = [
                build_humanitix_xero_preview(payout)
                for payout in payouts
            ]
        except (HumanitixPayoutImportError, UnicodeDecodeError) as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "payouts": [
                    serialize_humanitix_payout(payout, include_payload=True)
                    for payout in payouts
                ],
                "previews": previews,
                "posted_to_xero": False,
            }
        )


class HumanitixPayoutPreviewView(ReconciliationAdminView):
    def get(self, request, payout_reference: str):
        _, organization, error = self.context(request)
        if error:
            return error
        record = HumanitixPayout.objects.filter(
            organization=organization,
            payout_reference=payout_reference,
        ).first()
        if record is None:
            return Response(
                {"error": "Humanitix payout was not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        preview = build_humanitix_xero_preview(record)
        return Response(
            {
                "payout": serialize_humanitix_payout(record),
                "preview": preview,
            }
        )


class HumanitixPayoutPostView(ReconciliationAdminView):
    def post(self, request, payout_reference: str):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response(
                {"error": "confirm must be true to post to Xero"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record = HumanitixPayout.objects.filter(
            organization=organization,
            payout_reference=payout_reference,
        ).first()
        if record is None:
            return Response(
                {"error": "Humanitix payout was not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            posted = post_humanitix_xero_bank_transaction(
                record,
                approved_by_slack_id=slack_user_id,
            )
        except ReconciliationValidationError as exc:
            return Response(
                {"error": str(exc), "errors": exc.errors},
                status=status.HTTP_409_CONFLICT,
            )
        except XeroPostingError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "payout": serialize_humanitix_payout(
                    posted,
                    include_payload=True,
                )
            }
        )


class ReconciliationPayoutPreviewView(ReconciliationAdminView):
    def get(self, request, payout_id: str):
        _, organization, error = self.context(request)
        if error:
            return error
        record = StripePayoutReconciliation.objects.filter(organization=organization, payout_id=payout_id).first()
        if record is None:
            return Response({"error": "Payout was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            preview = build_xero_preview(record)
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        return Response({"payout": serialize_payout(record), "preview": preview})


class ReconciliationPayoutPostView(ReconciliationAdminView):
    def post(self, request, payout_id: str):
        slack_user_id, organization, error = self.context(request, from_body=True)
        if error:
            return error
        if request.data.get("confirm") is not True:
            return Response({"error": "confirm must be true to post to Xero"}, status=status.HTTP_400_BAD_REQUEST)
        record = StripePayoutReconciliation.objects.filter(organization=organization, payout_id=payout_id).first()
        if record is None:
            return Response({"error": "Payout was not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            posted = post_xero_bank_transaction(record, approved_by_slack_id=slack_user_id)
        except ReconciliationValidationError as exc:
            return Response({"error": str(exc), "errors": exc.errors}, status=status.HTTP_409_CONFLICT)
        except XeroPostingError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"payout": serialize_payout(posted, include_payload=True)})
