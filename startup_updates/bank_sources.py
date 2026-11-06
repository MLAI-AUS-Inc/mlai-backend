"""Bounded bank-feed collection with exact account and immutable cash evidence."""

from datetime import datetime, time
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from django.utils.dateparse import parse_date, parse_datetime

from integrations.models import ExternalServiceConnection
from integrations.services import external_connectors as ec
from startup_updates.activity_scope import activity_window
from startup_updates.evidence_contract import content_hash
from startup_updates.models import StartupEvent
from startup_updates.source_evidence import complete_source, stage_source


def fetch_bank_page(run, binding, state):
    """Read one page from the pinned Basiq user without database locks."""
    pin = (run.run_request.get("source_connection_pins") or {}).get("bank_feed")
    connection = (
        ExternalServiceConnection.objects.filter(
            pk=pin,
            user=binding.user,
            organization_id=run.organization_id,
            provider="bank_feed",
        )
        .exclude(status="disconnected")
        .first()
    )
    if connection is None or not connection.external_account_id:
        raise PermissionError("The selected bank account changed.")
    path = f"/users/{quote(connection.external_account_id, safe='')}/transactions"
    expected = urlparse(ec._basiq_api_url(path))
    cursor = state.get("cursor") or path
    actual = urlparse(ec._basiq_api_url(cursor))
    if (actual.scheme, actual.netloc, actual.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ) or actual.fragment:
        raise ValueError("Bank pagination left the selected account.")
    token = ec._basiq_token(scope="SERVER_ACCESS").get("access_token")
    if not token:
        raise PermissionError("Bank access is unavailable.")
    payload = ec._basiq_get_json(
        cursor,
        token,
        None
        if state.get("cursor")
        else {
            "filter": "status.eq('posted')",
            "limit": 100,
        },
    )
    if not isinstance(payload.get("data", payload.get("items")), list):
        raise TypeError("Bank transactions returned an invalid collection.")
    next_cursor = ec._basiq_next_link(payload)
    visited = state.get("visited", [])
    current = actual.geturl()
    if next_cursor:
        next_url = urlparse(ec._basiq_api_url(next_cursor)).geturl()
        if next_url == current or next_url in visited:
            raise ValueError("Bank pagination did not advance.")
        # Validate before saving the next pointer, including its account path.
        target = urlparse(next_url)
        if (target.scheme, target.netloc, target.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ) or target.fragment:
            raise ValueError("Bank pagination left the selected account.")
    return ec._basiq_collection_items(payload), {
        "cursor": next_cursor,
        "visited": [*visited, current],
        "complete": not bool(next_cursor),
    }


def commit_bank_page(run, rows, state):
    """Save dated cash facts only; bank transfers never become MRR or revenue."""
    zone = ZoneInfo(run.run_request.get("reporting_timezone") or "UTC")
    start, end = activity_window(run.run_request["narrative_period"])
    for row in rows:
        if str(row.get("status") or "posted").lower() != "posted":
            continue
        raw_date = str(
            row.get("transactionDate")
            or row.get("transaction_date")
            or row.get("date")
            or ""
        )
        day = parse_date(raw_date[:10])
        stamp = parse_datetime(
            str(
                row.get("postDate") or row.get("postedAt") or row.get("posted_at") or ""
            )
        )
        if day is None and stamp is not None and stamp.tzinfo is not None:
            day = stamp.astimezone(zone).date()
        if day is None:
            raise ValueError("A posted bank transaction has no verifiable date.")
        # Transaction dates have day precision. The half-open reporting window
        # is applied in the startup's timezone, including its final partial day.
        if not start.astimezone(zone).date() <= day <= end.astimezone(zone).date():
            continue
        if datetime.combine(day, time.min, zone) >= end:
            continue
        source_id = str(row.get("id") or row.get("transactionId") or "").strip()
        amount = ec._decimal_or_none(row.get("amount"))
        if not source_id or amount is None or not amount.is_finite():
            raise ValueError("A posted bank transaction has incomplete evidence.")
        direction = str(
            row.get("direction") or ("debit" if amount < 0 else "credit")
        ).lower()
        if direction not in {"debit", "credit"}:
            raise ValueError("A bank transaction has an unknown cash direction.")
        currency = str(row.get("currency") or "").strip().upper()
        if not currency or len(currency) > 12:
            raise ValueError("A bank transaction has no verifiable currency.")
        fact = {
            "id": source_id,
            "date": day.isoformat(),
            "amount": str(abs(amount)),
            "currency": currency,
            "direction": direction,
        }
        text = f"Posted bank {direction}: {currency} {abs(amount)} on {day.isoformat()}. This is cash movement, not accounting revenue, MRR, burn or runway."
        staged = stage_source(
            run,
            "bank_feed",
            source_id,
            {
                "cleaned_text": text,
                "transaction": fact,
                "connection_id": run.run_request["source_connection_pins"]["bank_feed"],
                "reporting_window": run.run_request["narrative_period"],
            },
        )
        if staged is not None:
            complete_source(
                run,
                "bank_feed",
                source_id,
                {
                    "source_fingerprint": staged["source_fingerprint"],
                    "facts": [fact],
                    "extraction_method": "bank_cash_v1",
                },
            )
        StartupEvent.objects.update_or_create(
            organization_id=run.organization_id,
            canonical_key=f"bank_feed:cash:{run.pk}:{content_hash({'id': source_id})}",
            defaults={
                "run": run,
                "event_type": "burn_or_runway",
                "title": "Posted bank cash movement",
                "summary": text,
                "event_date": day,
                "month_bucket": day.replace(day=1),
                "source_thread_ids": [f"bank_feed:{source_id}"],
                "evidence_message_ids": [f"bank_feed:{source_id}"],
                "confidence": 1.0,
                "needs_review": False,
            },
        )
    run.result = {
        **(run.result or {}),
        "event_source_progress": {
            **((run.result or {}).get("event_source_progress") or {}),
            "bank_feed": state,
        },
    }
