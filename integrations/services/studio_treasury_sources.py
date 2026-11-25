"""Scoped live reads for payment reviews, kept separate from usage-report merging."""
from datetime import datetime
from decimal import Decimal
import base64
import hashlib
import io
import re
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from integrations.models import ExternalServiceConnection, StudioMeetingOccurrence
from .external_connectors import _xero_get_json
from .linear_meeting_actions import _graphql, _list_linear_issue_comments
from .studio_payment_rules import ReviewError, cents, digest, normal_number, quantity
from .studio_report_source import all_invoices, mailbox_ids
from .studio_submissions import pdf_attachment, invoice_rows, sender_of, work_from_issue


SIZES = {"Extra Small (XS)": 900, "Small (S)": 3600, "Medium (M)": 7200,
         "Large (L)": 10800, "Extra Large (XL)": 18000}


class StudioSources:
    def __init__(self, ledger, artifacts=None):
        self.ledger = ledger
        self.config = ledger.configuration
        self.artifacts = artifacts or {}

    def sealed_claim(self, message, mailbox, text, builder):
        refs = set(re.findall(r"\bROO-[A-F0-9]{16}\b", text))
        if len(refs) != 1:
            raise ReviewError("sealed_invoice_reference_missing")
        artifact = self.artifacts[next(iter(refs))]
        doc = artifact["invoice"]
        if (doc["draft"] or doc["builder_id"] != builder or doc["team"] != self.ledger.workspace_id
                or doc["organization"] != self.config["linear_organization"]):
            raise ReviewError("sealed_invoice_scope_mismatch")
        parts = [message["payload"]]
        attachments = []
        while parts:
            part = parts.pop()
            parts.extend(part.get("parts", []))
            if part.get("mimeType") == "application/pdf":
                attachments.append(part["body"])
        if len(attachments) != 1:
            raise ReviewError("sealed_invoice_attachment_ambiguous")
        body = attachments[0]
        if "data" not in body:
            body = mailbox.users().messages().attachments().get(userId="me", messageId=message["id"], id=body["attachmentId"]).execute()
        reader = PdfReader(io.BytesIO(base64.urlsafe_b64decode(body["data"])))
        static = hashlib.sha256(b"".join(page.get_contents().get_data() for page in reader.pages)).hexdigest()
        if static != artifact["static_hash"]:
            raise ReviewError("sealed_invoice_work_changed")
        fields = reader.get_fields() or {}
        number = str(fields.get("invoice_number", {}).get("/V", "")).strip() or doc["reference"]
        lines = []
        for row in doc["rows"]:
            agreement = self.config["projects"][row["project_id"]]
            if agreement["basis"] != "size" or row["kind"] != "ticket" or row["units"] <= 0:
                raise ReviewError("sealed_correction_needs_review")
            day = datetime.fromisoformat(row["completed_at"]).astimezone(ZoneInfo("Australia/Melbourne")).date().isoformat()
            lines.append({"issue": row["identifier"], "project": row["project_id"], "start": day, "end": day,
                          "units": row["units"] * 900, "cents": row["units"] * 1625, "basis": "size"})
        return {"builder": builder, "number": number, "currency": doc["currency"], "cents": doc["total_cents"],
                "lines": lines, "reference": doc["reference"], "source_hash": digest(text)}

    def issues(self):
        cursor, rows = None, []
        for _ in range(100):
            data = _graphql("""query StudioTreasuryWork($projects:[ID!]!, $after:String) {
              organization { id }
              issues(first:100, after:$after, includeArchived:true,
                filter:{project:{id:{in:$projects}}}) {
                nodes { id identifier title description updatedAt completedAt
                  state {type} assignee {id} project {id} labels {nodes {name}} }
                pageInfo {hasNextPage endCursor}
              }}""", {"projects": list(self.config["projects"]), "after": cursor})
            if data["organization"]["id"] != self.config["linear_organization"]:
                raise ReviewError("linear_workspace_mismatch")
            page = data["issues"]
            rows.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return rows
            cursor = page["pageInfo"]["endCursor"]
        raise ReviewError("linear_page_limit")

    def approved_work(self, issues):
        rows = []
        meetings = list(StudioMeetingOccurrence.objects.filter(ledger=self.ledger))
        meeting_issue_ids = {row.linear_issue.get("id") for row in meetings}
        for issue in issues:
            project = (issue.get("project") or {}).get("id")
            builder = (issue.get("assignee") or {}).get("id")
            if (project not in self.config["projects"] or builder not in self.config["builders"]
                    or issue.get("state", {}).get("type") != "completed" or not issue.get("completedAt")):
                continue
            if issue["id"] in meeting_issue_ids:
                continue
            agreement = self.config["projects"][project]
            basis = agreement["basis"]
            normalized = dict(issue, description=re.sub(r"(?im)^Actual human time:", "Actual time:",
                (issue.get("description") or "").replace("**", "")))
            entry = work_from_issue(normalized)
            if basis == "actual":
                if entry is None:
                    continue
                _, start, end, microhours = entry
                units = quantity(Decimal(microhours) / 1000000)
            elif basis == "size":
                # Size credit is a contractual unit. It never becomes actual time.
                selected = set(x["name"] for x in issue.get("labels", {}).get("nodes", [])) & SIZES.keys()
                if len(selected) != 1:
                    continue
                units = SIZES[next(iter(selected))]
                if entry:
                    _, start, end, _ = entry
                else:
                    start = end = datetime.fromisoformat(issue["completedAt"].replace("Z", "+00:00")).astimezone(
                        ZoneInfo("Australia/Melbourne")).date().isoformat()
            else:
                continue
            if start < agreement["effective_from"] or end > agreement.get("effective_to", "9999-12-31"):
                continue
            rows.append({"key": issue["id"], "builder": builder, "project": project,
                         "issue": issue["identifier"], "start": start, "end": end, "units": units,
                         "pool": agreement["pool"], "basis": basis,
                         "approval": digest(issue), "agreement": agreement["version"]})
        live = {row["id"]: row for row in issues}
        size_meetings = set()
        for meeting in meetings:
            issue = live.get(meeting.linear_issue.get("id"))
            if not issue or issue.get("state", {}).get("type") != "completed":
                continue
            if not any(time.get("approved") for time in meeting.participation.values()):
                continue
            agreement = self.config["projects"][meeting.project_id]
            comments, _ = _list_linear_issue_comments(issue["id"])
            notes = {row["id"]: digest(row["body"]) for row in comments}
            for builder, time in meeting.participation.items():
                if (not time.get("approved") or time.get("comment_pending")
                        or not time.get("note_hash") or notes.get(time.get("comment_id")) != time["note_hash"]):
                    continue
                if agreement["basis"] == "size":
                    if builder != (issue.get("assignee") or {}).get("id") or issue["id"] in size_meetings:
                        continue
                    sizes = set(x["name"] for x in issue.get("labels", {}).get("nodes", [])) & SIZES.keys()
                    if len(sizes) != 1:
                        continue
                    units = SIZES[next(iter(sizes))]
                    size_meetings.add(issue["id"])
                else:
                    units = time["minutes"] * 60
                day = time["work_date"]
                work_key = "meeting:" + meeting.occurrence_key + ":" + builder
                if agreement["basis"] == "size":
                    day = datetime.fromisoformat(issue["completedAt"].replace("Z", "+00:00")).astimezone(
                        ZoneInfo("Australia/Melbourne")).date().isoformat()
                    work_key = issue["id"]
                if day < agreement["effective_from"] or day > agreement.get("effective_to", "9999-12-31"):
                    continue
                rows.append({"key": work_key,
                    "builder": builder, "project": meeting.project_id, "issue": issue["identifier"],
                    "start": day, "end": day, "units": units, "pool": agreement["pool"], "basis": agreement["basis"],
                    "approval": digest(time), "agreement": agreement["version"]})
        return rows

    def claims(self, selector):
        # A bounded full query detects revised invoices instead of trusting a saved PDF.
        config = self.config
        senders = {email.casefold(): builder for builder, item in config["builders"].items()
                   for email in item["senders"]}
        query = "after:" + config["invoice_since"] + " has:attachment -in:trash -in:spam {" + " ".join(
            "from:" + address for address in senders) + "}"
        mailbox, ids = mailbox_ids(config["mail_credentials_path"], query)
        if len(ids) > 100:
            raise ReviewError("invoice_scan_needs_narrower_period")
        groups, exceptions = {}, []
        projects = {key: value["name"] for key, value in config["projects"].items()}
        for mid in sorted(ids):
            message = mailbox.users().messages().get(userId="me", id=mid).execute()
            sender = sender_of(message)
            if sender not in senders:
                continue
            builder = senders[sender]
            try:
                text = pdf_attachment(mailbox, message)
                if re.search(r"\bROO-[A-F0-9]{16}\b", text):
                    claim = self.sealed_claim(message, mailbox, text, builder)
                    claim["source"] = "https://mail.google.com/mail/u/?authuser=treasurer%40mlai.au#all/" + mid
                    groups[builder + ":" + normal_number(claim["number"])] = claim
                    continue
                number, issued, parsed = invoice_rows(text, projects)
                lines = []
                for project, issue, start, end, microhours in parsed:
                    agreement = config["projects"][project]
                    units = quantity(Decimal(microhours) / 1000000)
                    # These recognised formats have a single explicit labour rate.
                    rates = set(re.findall(r"\$([\d,.]+)\s*(?:/hour|\s+\$)", text))
                    expected_rate = Decimal(agreement["builder_rate_cents"]) / 100
                    if not rates or any(Decimal(rate.replace(",", "")) != expected_rate for rate in rates):
                        raise ReviewError("invoice_rate_needs_review")
                    amount = int((Decimal(units) * agreement["builder_rate_cents"] / 3600).quantize(Decimal(1)))
                    lines.append({"project": project, "issue": issue, "start": start, "end": end,
                                  "units": units, "cents": amount, "basis": agreement["basis"]})
                total = sum(line["cents"] for line in lines)
                totals = re.findall(r"^\s*Total:\s*\$([\d,.]+)\s*$", text, re.M)
                if len(totals) != 1 or cents(totals[0].replace(",", "")) != total:
                    raise ReviewError("invoice_total_needs_review")
                claim = {"builder": builder, "number": number, "issued": issued, "currency": "AUD",
                         "cents": total, "lines": lines, "source_hash": digest(text),
                         "source": "https://mail.google.com/mail/u/?authuser=treasurer%40mlai.au#all/" + mid}
                key = builder + ":" + normal_number(number)
                if key in groups and groups[key]["source_hash"] != claim["source_hash"]:
                    raise ReviewError("invoice_revision_conflict")
                groups[key] = claim
            except (ValueError, ArithmeticError, KeyError):
                exceptions.append({"source": mid, "builder": builder, "code": "invoice_extraction_needs_review"})
        claims = [claim for claim in groups.values() if selector == "recent" or normal_number(claim["number"]) == normal_number(selector)]
        if not claims and not exceptions:
            raise ReviewError("invoice_not_found")
        return claims, exceptions

    def funding(self):
        config = self.config
        connection = ExternalServiceConnection.objects.select_for_update().get(id=config["xero_connection_id"],
            organization_id=self.ledger.organization_id, provider="xero")
        get = lambda path, **kw: _xero_get_json(connection, path, **kw)
        invoices = {row["InvoiceID"]: row for contact in config["customer_contacts"]
                    for row in all_invoices(get, contact)}
        result = []
        for spec in config["funding_lines"]:
            invoice = invoices.get(spec["invoice_id"])
            if not invoice or invoice.get("Type") != "ACCREC" or invoice.get("Status") not in {"AUTHORISED", "PAID"}:
                continue
            if Decimal(str(invoice.get("AmountCredited", 0))) != 0:
                continue  # A credited pool needs reviewed line attribution before reuse.
            line = next((row for row in invoice["LineItems"] if row.get("LineItemID") == spec["line_id"]), None)
            net = cents(line["LineAmount"]) if line else 0
            if line and invoice.get("LineAmountTypes") == "Inclusive":
                net -= cents(line.get("TaxAmount", 0))
            if (not line or invoice["CurrencyCode"] != spec["currency"] or line.get("ItemCode") != spec["item_code"]
                    or net != spec["net_cents"]):
                raise ReviewError("customer_invoice_changed")
            # The reviewed conversion is tied to this exact labour invoice line.
            result.append({"key": spec["invoice_id"] + ":" + spec["line_id"],
                           "pool": spec["pool"], "basis": spec["basis"], "start": spec["start"], "end": spec["end"],
                           "currency": spec["currency"], "units": quantity(spec["hours"]),
                           "cents": spec["net_cents"]})
        return result

    def snapshot(self, selector):
        claims, exceptions = self.claims(selector)
        work = self.approved_work(self.issues())
        funding = self.funding()
        exceptions.extend(self.unmatched_payments())
        return {"claims": claims, "work": work, "funding": funding, "exceptions": exceptions}

    def unmatched_payments(self):
        """Any imported manual debit must be accounted for before paying again."""
        connection = ExternalServiceConnection.objects.select_for_update().get(id=self.config["xero_connection_id"],
            organization_id=self.ledger.organization_id, provider="xero")
        known = {row.get("receipt", {}).get("id") for row in self.ledger.reservations.values()}
        payees = {row["xero_contact_id"]: builder for builder, row in self.config["builders"].items()}
        results, seen = [], set()
        for page in range(1, 101):
            payload = _xero_get_json(connection, "/BankTransactions", params={"page": page})
            rows = payload.get("BankTransactions")
            if not isinstance(rows, list):
                raise ReviewError("bank_feed_incomplete")
            for bank in rows:
                ident = bank.get("BankTransactionID")
                if not ident or ident in seen:
                    raise ReviewError("bank_feed_identity_changed")
                seen.add(ident)
                builder = payees.get(bank.get("Contact", {}).get("ContactID"))
                if (builder and bank.get("Type") == "SPEND" and bank.get("Status") == "AUTHORISED"
                        and ident not in known):
                    from .studio_report_source import xero_day
                    if xero_day(bank["Date"]) >= self.config["invoice_since"]:
                        results.append({"builder": builder, "source": ident, "code": "unmatched_manual_payment"})
            if len(rows) < 100:
                return results
        raise ReviewError("bank_feed_page_limit")

    def bank_receipt(self, receipt_id, reference, invoice):
        connection = ExternalServiceConnection.objects.select_for_update().get(id=self.config["xero_connection_id"],
            organization_id=self.ledger.organization_id, provider="xero")
        from uuid import UUID
        receipt_id = str(UUID(receipt_id))
        rows = _xero_get_json(connection, "/BankTransactions/" + receipt_id).get("BankTransactions", [])
        contact = self.config["builders"][invoice["invoice"].split(":", 1)[0]]["xero_contact_id"]
        if len(rows) != 1:
            raise ReviewError("bank_receipt_missing")
        bank = rows[0]
        if (bank.get("Type") != "SPEND" or bank.get("Status") != "AUTHORISED" or not bank.get("IsReconciled")
                or bank.get("Reference") != reference or bank.get("Contact", {}).get("ContactID") != contact
                or bank.get("CurrencyCode") != invoice["currency"] or cents(bank["Total"]) != invoice["cents"]):
            raise ReviewError("bank_receipt_mismatch")
        return {"id": receipt_id, "sha256": digest(bank)}
