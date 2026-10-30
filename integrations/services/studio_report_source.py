"""Private, read-only source preparation. No accounting writes or inferred hours.

The approved work manifest is reloaded on every preparation. Mail discovery
detects unreviewed invoices; it never converts money or prose into work hours.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Melbourne")


class SourceIncomplete(ValueError):
    pass


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cents(value):
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise SourceIncomplete("invalid_money")
    return int(amount)


def xero_day(value):
    if isinstance(value, str) and value.startswith("/Date("):
        match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", value)
        if not match:
            raise SourceIncomplete("invalid_xero_date")
        return datetime.fromtimestamp(int(match[1]) / 1000, timezone.utc).date().isoformat()
    return datetime.fromisoformat(value[:10]).date().isoformat()


def all_invoices(get, contact):
    result, seen = [], set()
    for page in range(1, 101):
        payload = get("/Invoices", params={"ContactIDs": contact, "page": page, "pageSize": 100})
        rows = payload.get("Invoices")
        if not isinstance(rows, list):
            raise SourceIncomplete("invoices_response_invalid")
        for row in rows:
            identity = row.get("InvoiceID")
            if not identity or identity in seen or row.get("Contact", {}).get("ContactID") != contact:
                raise SourceIncomplete("invoice_identity_mismatch")
            seen.add(identity)
            result.append(row)
        pagination = payload.get("pagination", {})
        count = pagination.get("pageCount")
        if (count is not None and page >= int(count)) or (count is None and len(rows) < 100):
            if pagination.get("itemCount") is not None and int(pagination["itemCount"]) != len(result):
                raise SourceIncomplete("invoice_pagination_incomplete")
            return result
        if not rows:
            raise SourceIncomplete("invoice_pagination_incomplete")
    raise SourceIncomplete("invoice_page_limit")


def refresh_finance(snapshot, owner, invoices, get, cutoff, legacy_invoice_ids=(), contact_id=None):
    """Replace the live ledger, including revisions, deleted payments and batches.

    Explicitly approved historical external invoices remain distinct. A missing
    previously mapped Xero invoice is a gap, never an assumed zero or deletion.
    """
    value = deepcopy(snapshot)
    data = value["clients"][owner]
    old_by_number = {row["number"]: row for row in data["invoices"]}
    live_numbers = {row["InvoiceNumber"] for row in invoices}
    legacy = set(legacy_invoice_ids)
    if any(row["number"] not in live_numbers and row["id"] not in legacy for row in data["invoices"]):
        raise SourceIncomplete("previous_invoice_missing")
    result = [row for row in data["invoices"] if row["id"] in legacy and row["number"] not in live_numbers]
    retained = {row["id"] for row in result}
    payments = []
    for row in data["payments"]:
        if any(part["invoice_id"] in retained for part in row["allocations"]):
            if not all(part["invoice_id"] in retained for part in row["allocations"]):
                raise SourceIncomplete("mixed_legacy_payment")
            # Some reviewed historical receipts are RECEIVE bank transactions,
            # not invoice Payments. Revalidate those too, including reversals.
            bank_rows = get("/BankTransactions/" + row["bank_transaction_id"]).get("BankTransactions")
            if not isinstance(bank_rows, list) or len(bank_rows) != 1:
                raise SourceIncomplete("legacy_receipt_missing")
            bank = bank_rows[0]
            if (bank.get("BankTransactionID") != row["bank_transaction_id"] or not contact_id
                    or bank.get("Contact", {}).get("ContactID") != contact_id
                    or bank.get("CurrencyCode") != "AUD" or bank.get("Type") != "RECEIVE"):
                raise SourceIncomplete("legacy_receipt_scope_mismatch")
            evidence = "xero-bank-" + row["bank_transaction_id"]
            value["evidence"][evidence] = {"reference": "Xero bank receipt " + row["bank_transaction_id"], "sha256": digest(bank)}
            if bank.get("Status") == "DELETED":
                continue
            if (bank.get("Status") != "AUTHORISED" or not bank.get("IsReconciled")
                    or cents(bank["Total"]) != row["amount_cents"] or xero_day(bank["Date"]) != row["date"]):
                raise SourceIncomplete("legacy_receipt_changed")
            payments.append({**row, "evidence_ids": list(dict.fromkeys([*row["evidence_ids"], evidence]))})
    for legacy_row in result:
        legacy_row["paid_cents"] = sum(part["amount_cents"] for p in payments for part in p["allocations"]
                                       if part["invoice_id"] == legacy_row["id"])
        legacy_row["status"] = "paid" if legacy_row["paid_cents"] == legacy_row["amount_cents"] else "authorised"
    groups, seen_payments = {}, set()
    last_day = instant(cutoff).astimezone(TZ).date().isoformat()
    for invoice in invoices:
        if invoice.get("Type") != "ACCREC" or invoice.get("CurrencyCode") != "AUD":
            raise SourceIncomplete("invoice_currency_or_type")
        status = invoice["Status"].lower()
        if status == "deleted":
            status = "voided"
        if status == "submitted":
            status = "draft"
        if status not in {"paid", "authorised", "draft", "voided"}:
            raise SourceIncomplete("invoice_status_unknown")
        number = invoice["InvoiceNumber"]
        identity = old_by_number.get(number, {}).get("id", invoice["InvoiceID"])
        evidence_id = "xero-invoice-" + invoice["InvoiceID"]
        value["evidence"][evidence_id] = {"reference": "Xero invoice " + invoice["InvoiceID"], "sha256": digest(invoice)}
        paid = 0
        for stub in invoice.get("Payments", []):
            pid = stub["PaymentID"]
            if pid in seen_payments:
                raise SourceIncomplete("duplicate_payment")
            seen_payments.add(pid)
            rows = get("/Payments/" + pid).get("Payments")
            if not isinstance(rows, list) or len(rows) != 1:
                raise SourceIncomplete("payment_missing")
            payment = rows[0]
            if payment.get("PaymentID") != pid or payment.get("Invoice", {}).get("InvoiceID") != invoice["InvoiceID"]:
                raise SourceIncomplete("payment_invoice_mismatch")
            if payment.get("Status") == "DELETED":
                continue
            if payment.get("Status") != "AUTHORISED" or payment.get("PaymentType") != "ACCRECPAYMENT":
                raise SourceIncomplete("payment_status_unknown")
            if not payment.get("IsReconciled"):
                raise SourceIncomplete("payment_receipt_requires_review")
            amount = cents(payment["Amount"])
            if amount != cents(stub["Amount"]):
                raise SourceIncomplete("payment_revision_during_read")
            day = xero_day(payment["Date"])
            if day == last_day:
                # A retry may discover a later same-day receipt. A posting date
                # alone cannot establish it existed before the frozen 17:00.
                changed = payment.get("UpdatedDateUTC")
                if not changed or not changed.startswith("/Date("):
                    raise SourceIncomplete("payment_cutoff_time_unknown")
                millis = re.match(r"/Date\((\d+)", changed)
                if not millis or datetime.fromtimestamp(int(millis[1]) / 1000, timezone.utc) > instant(cutoff):
                    raise SourceIncomplete("payment_after_cutoff_requires_review")
            paid += amount
            if day > last_day:
                continue
            evidence = "xero-payment-" + pid
            value["evidence"][evidence] = {"reference": "Xero payment " + pid, "sha256": digest(payment)}
            batch = payment.get("BatchPayment", {}).get("BatchPaymentID") or stub.get("BatchPaymentID") or pid
            group = groups.setdefault(batch, {"id": batch, "bank_transaction_id": batch, "date": day,
                "amount_cents": 0, "description": "Verified Xero client receipt", "allocations": [],
                "invoice_ids": [], "evidence_ids": []})
            if group["date"] != day or identity in group["invoice_ids"]:
                raise SourceIncomplete("batch_payment_ambiguous")
            group["amount_cents"] += amount
            group["allocations"].append({"invoice_id": identity, "amount_cents": amount})
            group["invoice_ids"].append(identity)
            group["evidence_ids"].append(evidence)
        # Credits/overpayments are not cash receipts. Do not turn AmountPaid
        # into purchased hours if actual receipt attribution cannot reconcile.
        if paid != cents(invoice.get("AmountPaid", 0)) or cents(invoice.get("AmountCredited", 0)):
            raise SourceIncomplete("cash_allocation_requires_review")
        day = xero_day(invoice.get("DateString") or invoice["Date"])
        if day > last_day:
            continue
        cutoff_paid = sum(part["amount_cents"] for group in groups.values() for part in group["allocations"]
                          if part["invoice_id"] == identity)
        if status == "paid" and cutoff_paid != cents(invoice["Total"]):
            status = "authorised"
        result.append({"id": identity, "number": number, "date": day, "status": status,
            "amount_cents": cents(invoice["Total"]), "paid_cents": cutoff_paid, "evidence_ids": [evidence_id]})
    data["invoices"] = result
    data["payments"] = payments + list(groups.values())
    return value


def mailbox_ids(credentials_path, query):
    """Full bounded query on each pass; a lost/expired cursor cannot hide mail."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    from httplib2 import Http
    credentials = json.loads(Path(credentials_path).read_text())
    oauth = Credentials(
        token=None, refresh_token=credentials["refresh_token"],
        client_id=credentials["client_id"], client_secret=credentials["client_secret"],
        token_uri="https://oauth2.googleapis.com/token")
    service = build("gmail", "v1", http=AuthorizedHttp(oauth, http=Http(timeout=20)), cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    if profile["emailAddress"].lower() != credentials["email"].lower():
        raise SourceIncomplete("mailbox_identity_mismatch")
    token, ids = None, set()
    deadline = time.monotonic() + 60
    for _ in range(100):
        if time.monotonic() >= deadline:
            raise SourceIncomplete("mailbox_time_limit")
        payload = service.users().messages().list(userId="me", q=query, maxResults=500,
                                                  pageToken=token, includeSpamTrash=True).execute()
        ids.update(row["id"] for row in payload.get("messages", []))
        token = payload.get("nextPageToken")
        if not token:
            return ids
    raise SourceIncomplete("mailbox_page_limit")


def save_candidate(scope, request, response, snapshot, unknown_mail):
    """Keep review evidence backend-private; no raw mail or amounts in logs."""
    directory = scope.get("review_directory")
    if not directory:
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = digest({"owner": request["owner"], "cutoff": request["cutoff"]})
    fd, temporary = tempfile.mkstemp(dir=root, prefix=identity + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"result": response, "candidate_snapshot": snapshot,
                       "unreviewed_gmail_ids": sorted(unknown_mail)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / (identity + ".json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(scope, request, *, get, mail, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = instant(request["cutoff"])
    owner = request["owner"]
    manifest = json.loads(Path(scope["work_manifest"]).read_text())
    snapshot = manifest["snapshot"]
    data = snapshot["clients"][owner]
    if (snapshot["team"] != request["team"] or snapshot["organization"] != scope["linear_organization"]
            or set(data["project_ids"]) != set(scope["project_ids"]) or set(snapshot["clients"]) != {owner}):
        raise SourceIncomplete("work_scope_mismatch")
    missing, checks, unknown = [], {}, set()
    # reviewed_at means the work was reviewed, not merely read by this job.
    hours_complete = instant(manifest["hours_complete_through"])
    reviewed = instant(manifest["reviewed_at"])
    if hours_complete > reviewed or reviewed > now:
        raise SourceIncomplete("work_review_time_invalid")
    checks["hours"] = {"checked_at": now.isoformat(), "complete_through": hours_complete.isoformat(),
                        "reviewed_at": reviewed.isoformat()}
    if hours_complete < cutoff:
        missing.append("hours_coverage_missing")
    if data.get("pending_work") or any(row["unresolved_hour_units"] for row in data["contractor_invoices"]):
        missing.append("work_allocation_requires_review")
    try:
        ids = mail(scope["gmail_credentials"], scope["gmail_query"])
        unknown = ids - set(manifest.get("reviewed_gmail_ids", []))
        checks["contractor_invoices"] = {"checked_at": now.isoformat(), "unreviewed_count": len(unknown),
                                        "message_set_digest": digest(sorted(ids))}
        if unknown:
            missing.append("new_invoices_require_review")
    except Exception:
        missing.append("contractor_invoice_sync_failed")
    try:
        invoices = all_invoices(get, scope["xero_contact_id"])
        snapshot = refresh_finance(snapshot, owner, invoices, get, request["cutoff"],
                                   scope.get("legacy_invoice_ids", []), scope["xero_contact_id"])
        if digest(invoices) != digest(all_invoices(get, scope["xero_contact_id"])):
            raise SourceIncomplete("finance_changed_during_refresh")
        checks["client_invoices"] = {"checked_at": now.isoformat(), "count": len(invoices)}
        checks["payments"] = {"checked_at": now.isoformat(), "count": len(snapshot["clients"][owner]["payments"])}
    except Exception as exc:
        missing.append(str(exc) if isinstance(exc, SourceIncomplete) else "finance_sync_failed")
    if now < cutoff:
        missing.append("closing_refresh_required")
    response = {"version": 1, "owner": owner, "team": request["team"], "organization": scope["linear_organization"],
        "project_ids": sorted(scope["project_ids"]), "cutoff": request["cutoff"], "nonce": request["nonce"],
        "checked_at": now.isoformat(), "checks": checks, "missing": sorted(set(missing)),
        "status": "waiting_data" if missing else "ready"}
    if not missing:
        dataset = snapshot["clients"][owner]
        # Only extend the common envelope after independently verified work,
        # mailbox and finance coverage have ALL passed the closing barrier.
        dataset["coverage"]["through"] = max(dataset["coverage"]["through"], cutoff.astimezone(TZ).date().isoformat())
        dataset["as_of"] = now.isoformat()
        response["snapshot"] = snapshot
    save_candidate(scope, request, response, snapshot, unknown)
    return response
