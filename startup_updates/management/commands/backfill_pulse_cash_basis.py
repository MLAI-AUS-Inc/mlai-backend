"""Backfill Xero cash (Bank Summary) figures for Pulse charts saved before cash existed.

Saved revisions are immutable, so nothing here edits them. For each chart saved
with only accrual figures, this command fetches the Bank Summary for its six
months from the same Xero account and stores it as a content-addressed cash
supplement beside the chart's source evidence. Readers then show cash for that
chart (see ``upgrade_legacy_history``). Accrual corrections (Other Income and
every expense section) need no backfill: they are recomputed from the reports
already saved with each chart.

Dry run by default. Xero's daily API limit is shared with every other Xero
feature, so calls are paced and capped, and a rerun skips finished charts.

    python manage.py backfill_pulse_cash_basis                      # report only
    python manage.py backfill_pulse_cash_basis --apply --max-calls 200 --pause 2
"""
import calendar
import copy
import time
from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from integrations.services.external_connectors import ConnectorRateLimitError, fetch_xero_accounting_report
from integrations.services.xero_scopes import xero_has_cash_report_scope
from startup_updates.evidence_contract import content_hash
from startup_updates.financial_history import CASH_SUPPLEMENT_KIND, _cash_supplement, _month_shift
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateRevision
from startup_updates.services import _parse_xero_bank_summary_report


def _month_end(month):
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def legacy_charts(organization_id=None):
    """Distinct (organization, month, source snapshot) behind accrual-only charts."""
    revisions = MonthlyUpdateRevision.objects.filter(structured_memo__financial_history__snapshot__schemaVersion=1)
    if organization_id:
        revisions = revisions.filter(draft__organization_id=organization_id)
    seen = {}
    for revision in revisions.select_related("draft").order_by("pk").iterator():
        history = revision.structured_memo.get("financial_history") or {}
        source, frozen = history.get("_source") or {}, history.get("snapshot") or {}
        if type(source.get("snapshotId")) is not int or not isinstance(source.get("snapshotHash"), str):
            continue
        key = (revision.draft.organization_id, revision.draft.month, source["snapshotHash"])
        seen.setdefault(key, {"snapshotId": source["snapshotId"], "cutoff": frozen.get("cutoff")})
    return seen


class Command(BaseCommand):
    help = "Fetch Xero Bank Summary figures so Pulse charts saved before cash basis can show cash in and out."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Fetch from Xero and store supplements. Without it, only report.")
        parser.add_argument("--organization", type=int, help="Limit to one organization id.")
        parser.add_argument("--max-calls", type=int, default=200, help="Stop after this many Xero calls (default 200).")
        parser.add_argument("--pause", type=float, default=2.0, help="Seconds between Xero calls (default 2).")

    def handle(self, *args, **options):
        charts = legacy_charts(options["organization"])
        calls, stored, skipped, failed = 0, 0, 0, 0
        cache = {}
        self.stdout.write(f"{len(charts)} accrual-only chart source(s) found.")
        for (organization_id, month, source_hash), detail in charts.items():
            if _cash_supplement(organization_id, month, source_hash):
                skipped += 1
                continue
            snapshot = MonthlyEvidenceSnapshot.objects.filter(pk=detail["snapshotId"], content_hash=source_hash,
                organization_id=organization_id).first()
            plan = self._plan(snapshot, month, detail["cutoff"])
            if plan is None:
                failed += 1
                self.stdout.write(f"  org {organization_id} {month:%Y-%m}: no verifiable Xero account in its evidence; left as accrual.")
                continue
            connection = ExternalServiceConnection.objects.filter(pk=plan["connection_id"], organization_id=organization_id,
                provider="xero").exclude(status="disconnected").first()
            if connection is None or not connection.access_token or not xero_has_cash_report_scope(connection.scopes):
                failed += 1
                self.stdout.write(f"  org {organization_id} {month:%Y-%m}: Xero must be reconnected with the Bank Summary scope.")
                continue
            if not options["apply"]:
                self.stdout.write(f"  org {organization_id} {month:%Y-%m}: would fetch up to {len(plan['months'])} Bank Summary reports.")
                continue
            rows, reports = [], {}
            try:
                for start, end in plan["months"]:
                    key = (connection.pk, start, end)
                    if key not in cache:
                        if calls >= options["max_calls"]:
                            raise _Stop()
                        if calls:
                            time.sleep(options["pause"])
                        cache[key] = fetch_xero_accounting_report(connection, "BankSummary",
                            params={"fromDate": start.isoformat(), "toDate": end.isoformat()})
                        calls += 1
                    report = cache[key]
                    digest = content_hash(report)
                    reports[digest] = copy.deepcopy(report)
                    cash = _parse_xero_bank_summary_report(report)
                    evidence = {"unit": plan["currency"], "source_provider": "xero", "accounting_basis": "cash",
                        "connection_id": connection.pk, "timezone": plan["timezone"], "report_hash": digest,
                        "report_start_date": start.isoformat(), "report_end_date": end.isoformat()}
                    rows.append({"month": start.isoformat(),
                        "cash_in": str(cash["cash_in"]) if cash["cash_in"] is not None else None,
                        "cash_out": str(cash["cash_out"]) if cash["cash_out"] is not None else None,
                        "metric_evidence": {"cash_in": {**evidence, "source_metric": "xero_bank_summary_cash_in"},
                            "cash_out": {**evidence, "source_metric": "xero_bank_summary_cash_out"}}})
            except _Stop:
                self.stdout.write(f"Stopped at the {options['max_calls']}-call limit. Rerun to continue.")
                break
            except ConnectorRateLimitError:
                self.stdout.write("Xero's API limit was reached. Rerun after it resets.")
                break
            except Exception as exc:  # One account's failure must not stop the rest.
                failed += 1
                self.stdout.write(f"  org {organization_id} {month:%Y-%m}: Bank Summary failed ({type(exc).__name__}).")
                continue
            payload = {"kind": CASH_SUPPLEMENT_KIND, "organization_id": organization_id, "month": month.isoformat(),
                "source_snapshot_hash": source_hash, "fetched_at": timezone.now().isoformat(),
                "charts": {"cash": rows, "source_reports": reports}}
            MonthlyEvidenceSnapshot.objects.get_or_create(organization_id=organization_id, content_hash=content_hash(payload),
                defaults={"month": month, "payload": payload})
            stored += 1
            self.stdout.write(f"  org {organization_id} {month:%Y-%m}: stored cash for {len(rows)} months.")
        self.stdout.write(f"Done. Stored {stored}, already done {skipped}, not possible {failed}, Xero calls {calls}."
            + ("" if options["apply"] else " Dry run: nothing fetched or stored."))

    @staticmethod
    def _plan(snapshot, month, cutoff):
        """The six windows to fetch, using the account and coverage the chart used."""
        payload = snapshot.payload if snapshot else None
        if not isinstance(payload, dict):
            return None
        charts, period = payload.get("charts") or {}, payload.get("period") or {}
        evidence = [((row or {}).get("metric_evidence") or {}).get(field) or {}
            for row in charts.get("performance") or [] for field in ("income", "expenses")]
        evidence += [(metric.get("metadata") or {}) for metric in payload.get("metrics") or [] if isinstance(metric, dict)]
        accounts = {item.get("connection_id") for item in evidence if item.get("source_provider", "xero") == "xero"
            and isinstance(item.get("connection_id"), int)}
        if len(accounts) != 1 or not charts.get("currency") or not period.get("timezone"):
            return None
        try:
            final = date.fromisoformat(cutoff) if cutoff else _month_end(month)
        except ValueError:
            return None
        months = []
        for offset in range(-5, 1):
            start = _month_shift(month, offset)
            months.append((start, min(_month_end(start), final)))
        return {"connection_id": accounts.pop(), "currency": charts["currency"], "timezone": period["timezone"], "months": months}


class _Stop(Exception):
    pass
