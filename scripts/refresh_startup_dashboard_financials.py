"""Owner-authorized historical Xero refresh into a separate private approval.

Run inside the configured backend runtime only after the owner requests this
repair. No migrations, provider writes, rewards, or shared revisions are changed.
The creation key makes retries idempotent. Financial source failures fail closed.
"""
import argparse
import calendar
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from uuid import UUID


def verified_totals(report, month, parsed):
    """Reject a different period or missing/nonfinite canonical report totals."""
    end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    expected = f"1 {month:%B %Y} to {end.day} {end:%B %Y}"
    titles = " ".join(str(title) for item in report.get("Reports", [])
        for title in item.get("ReportTitles", []))
    if expected not in titles:
        raise ValueError("Xero did not return the requested full reporting month.")
    result = {}
    for key, report_key in (("revenue", "revenue"), ("monthlyCosts", "monthly_costs")):
        entry = parsed.get(report_key)
        amount = Decimal(str(entry["amount"])) if entry is not None else None
        if amount is None or not amount.is_finite():
            raise ValueError(f"Xero did not return a reliable {key} total.")
        result[key] = amount
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=int, required=True)
    parser.add_argument("--actor-id", type=int, required=True)
    parser.add_argument("--month", type=date.fromisoformat, required=True)
    parser.add_argument("--creation-key", type=UUID, required=True)
    args = parser.parse_args()
    month = args.month
    if month.day != 1 or month >= date.today().replace(day=1):
        raise ValueError("Choose the first day of a completed historical month.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from community_chat.startups.presentation import update_payload
    from community_chat.startups.publication import approved_updates
    from founder_tools.models import VibeRaisingCompany
    from integrations.models import ExternalServiceConnection
    from integrations.services.external_connectors import fetch_xero_accounting_report, fetch_xero_base_currency
    from organizations.models import Organization
    from startup_updates.evidence_contract import content_hash, financial_snapshot_from_metrics, reporting_period, REVENUE_DEFINITION
    from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft, StartupMetricObservation, StartupProfile
    from startup_updates.revisions import save_revision, approve_and_publish
    from startup_updates.services import _parse_xero_profit_and_loss_report, _xero_report_metadata
    from startup_updates.update_identity import resolve_update

    actor = get_user_model().objects.get(pk=args.actor_id)
    organization = Organization.objects.get(pk=args.organization_id)
    if not VibeRaisingCompany.objects.filter(organization=organization, profile__user=actor).exists():
        raise ValueError("The approval actor does not own this startup.")
    existing = MonthlyUpdateDraft.objects.filter(organization=organization, creation_key=args.creation_key).first()
    if existing:
        if existing.month != month or not existing.published_revision_id or existing.published_revision.audience != "private":
            raise ValueError("The creation key already belongs to another operation.")
        print(json.dumps({"reused": True, "update_id": existing.pk, "revision_id": existing.published_revision_id}))
        return
    profile = StartupProfile.objects.get(organization=organization)
    connection = ExternalServiceConnection.objects.get(organization=organization,
        user=actor, provider="xero", status="connected")
    currency = fetch_xero_base_currency(connection)
    if currency != profile.default_currency:
        raise ValueError("The verified Xero currency differs from the startup currency.")
    end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    report = fetch_xero_accounting_report(connection, "ProfitAndLoss", params={
        "fromDate": month.isoformat(), "toDate": end.isoformat(),
        "paymentsOnly": "false", "standardLayout": "true"})
    parsed = _parse_xero_profit_and_loss_report(report)
    totals = verified_totals(report, month, parsed)
    fetched_at = timezone.now()

    def shared_fingerprint():
        rows = approved_updates("community", "public").filter(organization=organization).order_by("pk")
        return content_hash({"updates": [{"id": row.pk,
            "revision_id": row.published_revision_id,
            "revision_hash": row.published_revision.content_hash,
            "snapshot_hash": row.published_revision.snapshot.content_hash,
            "payload": update_payload(row, published=True, community=True)} for row in rows]})

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        # A concurrent retry must not fetch or approve a second monthly copy.
        if MonthlyUpdateDraft.objects.filter(organization=organization, creation_key=args.creation_key).exists():
            raise ValueError("This refresh completed concurrently; retry with the same creation key.")
        before = shared_fingerprint()
        metrics = []
        for key, report_key, label, source_metric, basis in (
            ("revenue", "revenue", "Revenue", "xero_profit_and_loss_revenue", "profit_and_loss_total_income"),
            ("monthlyCosts", "monthly_costs", "Monthly costs", "xero_profit_and_loss_monthly_costs", "cost_of_sales_plus_operating_expenses_when_available_otherwise_total_expenses"),
        ):
            entry = parsed[report_key]
            metadata = _xero_report_metadata(source_metric=source_metric, warnings=[],
                report_name="ProfitAndLoss", start_date=month, end_date=end, entry=entry,
                extra={"connection_id": connection.pk, "source_currency": currency,
                    "report_hash": content_hash(report), "report_payload": report,
                    "accounting_basis": "accrual", "fetched_at": fetched_at.isoformat(),
                    "calculation_basis": basis, "component_labels": entry.get("component_labels", []),
                    "component_amounts": entry.get("component_amounts", []),
                    "review_method": "Owner-requested private dashboard financial refresh"})
            display = f"{currency} {totals[key]:.2f}"
            observation, _ = StartupMetricObservation.objects.update_or_create(
                organization=organization, source_thread=None, source_provider="xero",
                metric_key=key, period_month=month, unit=currency,
                defaults={"run": None, "metric_name": label, "value_text": display,
                    "value_number": totals[key], "observed_at": fetched_at, "confidence": 1,
                    "evidence_message_ids": [], "evidence_attachment_ids": [], "source_record_ids": [],
                    "source_metadata": metadata, "summary": "Fresh full-month Xero Profit and Loss total."})
            metrics.append({"key": key, "label": label, "value": str(totals[key]),
                "display_value": display, "unit": currency, "quality": "source_reported",
                "observation_id": observation.pk, "observed_at": fetched_at.isoformat(),
                "source_provider": "xero", "source_record_ids": [], "metadata": metadata})
        payload = financial_snapshot_from_metrics(reporting_period(month, profile.reporting_timezone), metrics, [REVENUE_DEFINITION])
        payload.pop("hash", None)
        payload.update(organization_id=organization.pk, startup={"id": organization.pk, "name": organization.name},
            source_providers=["xero"], events=[], manual_sources={}, charts=None,
            config_version=profile.reporting_config_version,
            refresh_authorization={"actor_id": actor.pk, "purpose": "Private dashboard financial refresh", "creation_key": str(args.creation_key)})
        payload["hash"] = content_hash(payload)
        snapshot, _ = MonthlyEvidenceSnapshot.objects.get_or_create(organization=organization,
            content_hash=payload["hash"], defaults={"month": month, "payload": payload})
        draft, created = resolve_update(organization, month=month, creation_key=args.creation_key, update_date=end)
        if not created:
            raise ValueError("Refusing to overwrite an existing update.")
        memo = {"summary": "Private financial evidence refreshed from the full-month Xero Profit and Loss report. Existing shared updates are unchanged.",
            "selected_input_sources": ["xero"], "display_config": {"full_metric_keys": [], "snippet_metric_keys": []}}
        revision = save_revision(draft, memo, snapshot=snapshot, audience="private",
            require_match=False, validation={"groundedness_status": "founder_asserted",
                "notes": "Owner-authorized financial refresh; deterministic source totals. No public republication or AI verification."})
        approved = approve_and_publish(draft, actor=actor, revision_id=revision.pk,
            revision_hash=revision.content_hash, audience_visibility=["just_me"])
        after = shared_fingerprint()
        if before != after:
            raise ValueError("Shared publication changed; the entire private repair has been rolled back.")
        print(json.dumps({"reused": False, "organization_id": organization.pk,
            "update_id": approved.pk, "revision_id": revision.pk, "snapshot_id": snapshot.pk,
            "month": month.isoformat(), "currency": currency, "totals": {k: str(v) for k,v in totals.items()},
            "report_hash": content_hash(report), "shared_before": before, "shared_after": after,
            "audience": "just_me", "rewards_issued": False}))


if __name__ == "__main__":
    main()
