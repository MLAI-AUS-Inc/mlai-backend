"""Read explicit submitted work from scoped Linear records and authenticated invoice mail."""

import base64
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from email.utils import parseaddr
import io
import re

from django.conf import settings
from pypdf import PdfReader
from pypdf.errors import PdfReadError
import requests

from .studio_report_source import SourceIncomplete, digest, instant, TZ

ISSUE = r"[A-Z][A-Z0-9]*-\d+"
MONTH = r"[A-Za-z]{3,9}"
DAY = rf"(\d{{1,2}})(?:\s+({MONTH}))?(?:\s+(\d{{4}}))?"
NUMBER = r"\d+(?:\.\d+)?"


def period(text, year=None):
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text.strip(), flags=re.I)
    text = re.sub(r"\s*\(exact date not supplied\)$", "", text)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = date.fromisoformat(text).isoformat()
        return day, day
    parts = re.split(r"\s*(?:[–—-]|\bto\b)\s*", text)
    if len(parts) > 2 or not all(re.fullmatch(DAY, part) for part in parts):
        raise ValueError("invalid_work_period")
    first, last = (re.fullmatch(DAY, part).groups() for part in (parts[0], parts[-1]))
    year = int(last[2] or year or 0)

    def make(parts, month, year):
        return date(int(parts[2] or year), datetime.strptime(month[:3], "%b").month, int(parts[0]))

    if not last[1]:
        raise ValueError("missing_work_month")
    end = make(last, last[1], year)
    start = make(first, first[1] or last[1], year)
    if start > end or (end - start).days > 366:
        raise ValueError("invalid_work_period")
    return start.isoformat(), end.isoformat()


def units(hours):
    value = Decimal(hours) * 1000000
    if value <= 0 or value != value.to_integral_value() or value > 10**12:
        raise ValueError("invalid_hours")
    return int(value)


def invoice_rows(text, projects):
    """Recognized invoice layouts require explicit hours, project and work periods."""
    text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    headings = re.findall(r"^(?:MLAI Studio\s*[-–]\s*|\[Studio\]\s*)([^\n$]+)", text, re.M | re.I)
    if not headings or any(
        not any(
            h.strip().casefold() == name.casefold()
            or h.casefold().startswith(name.casefold() + " - completed work")
            for name in projects.values()
        )
        for h in headings
    ):
        raise ValueError("invoice_scope")
    projects_found = [
        key
        for key, name in projects.items()
        if re.search(
            r"^(?:MLAI Studio\s*[-–]\s*|\[Studio\]\s*)" + re.escape(name) + r"(?=\s*[-$]|$)",
            text,
            re.M | re.I,
        )
    ]
    if len(projects_found) != 1:
        raise ValueError("invoice_scope")
    project = projects_found[0]
    heading = re.search(r"^no\.\s*([\w-]+)\s*[–-]\s*(\d{1,2}(?:st|nd|rd|th)? \w+ (\d{4}))", text, re.I | re.M)
    if heading:
        number, issued, year = heading.groups()
        lines = re.findall(rf"^({NUMBER}) hours? @ \$[\d.,]+/hour\s*\(([^)]+)\)$", text, re.I | re.M)
        # Aggregate layouts are accepted only with one quantity and one priced project line.
        priced = re.findall(r"^(?!Total |Total$).+\s\$[\d,]+(?:\.\d+)?$", text, re.M | re.I)
        if len(lines) != 1 or len(priced) != 1 or projects[project].lower() not in priced[0].lower():
            raise ValueError("ambiguous_invoice_lines")
        h, dates = lines[0]
        return number, period(issued)[0], [(project, None, *period(dates, year), units(h))]
    headers = set(re.findall(r"^INVOICE\s+#\s*([\w-]+)$", text, re.M | re.I))
    if len(headers) != 1 or not re.search(r"^\[Studio\] ", text, re.M):
        raise ValueError("unknown_invoice_layout")
    date_line = re.search(r"Bill To: Date: (\w+ \d+, \d{4})", text)
    if not date_line:
        raise ValueError("missing_invoice_date")
    issued = datetime.strptime(date_line[1], "%b %d, %Y").date()
    rows = []
    for item in re.split(rf"^(?={ISSUE}\s+-\s+)", text, flags=re.M)[1:]:
        line = re.match(rf"^({ISSUE})\s+-\s+[^\n]+?\s+({NUMBER}) \$[\d,.]+ \$[\d,.]+", item)
        if not line:
            raise ValueError("invoice_item_invalid")
        dates = next((v for v in item.splitlines()[1:5] if re.match(r"^\d{1,2}(?:[-– ]|$)", v)), "")
        rows.append((project, line[1], *period(dates), units(line[2])))
    total = re.search(r"^Total hours:\s*(\d+)h(?:\s+(\d+)m)?$", text, re.M | re.I)
    if (
        not rows
        or not total
        or sum(row[-1] for row in rows) * 60 != (int(total[1]) * 60 + int(total[2] or 0)) * 1000000
    ):
        raise ValueError("invoice_total_mismatch")
    if len({row[1] for row in rows}) != len(rows):
        raise ValueError("repeated_invoice_item")
    return headers.pop(), issued.isoformat(), rows


def linear_issues(scope, since):
    cursor = None
    query = """query($filter: IssueFilter!, $cursor: String) {
      organization { id }
      issues(filter:$filter, first:50, after:$cursor, includeArchived:true) {
        nodes { identifier description updatedAt completedAt state { type } project { id } assignee { id } }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    for _ in range(100):
        response = requests.post(
            "https://api.linear.app/graphql",
            timeout=(3, 15),
            headers={"Authorization": settings.LINEAR_API_KEY},
            json={
                "query": query,
                "variables": {
                    "filter": {"project": {"id": {"in": scope["project_ids"]}}, "updatedAt": {"gte": since}},
                    "cursor": cursor,
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors") or payload["data"]["organization"]["id"] != scope["linear_organization"]:
            raise SourceIncomplete("submission_linear_check_failed")
        page = payload["data"]["issues"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]
    raise SourceIncomplete("submission_linear_page_limit")


def work_from_issue(issue):
    body = (issue.get("description") or "").replace("**", "")
    hours = re.search(
        r"^(?:Actual time(?: reported by [^:\n]+)?|Retrospective time allocation|Logged time|Time spent):\s*([\d.]+) (minutes?|hours?)\b",
        body,
        re.M | re.I,
    )
    if not hours:
        return None
    number = re.search(r"^## Invoice\s+#?([\w-]+)\b", body, re.M | re.I)
    dates = re.search(r"^(?:Work date|Billing period): *([^\n]+)", body, re.M | re.I)
    if not dates:
        raise ValueError("missing_work_date")
    amount = Decimal(hours[1])
    if hours[2].lower().startswith("minute"):
        amount = (amount / 60).quantize(Decimal(".000001"))
    return (number[1] if number else None, *period(dates[1]), units(amount))


def pdf_attachment(service, msg):
    parts = [msg["payload"]]
    pdfs = []
    while parts:
        part = parts.pop()
        parts.extend(part.get("parts", []))
        if part.get("mimeType") == "application/pdf":
            pdfs.append(part["body"])
    if len(pdfs) != 1 or not 0 < pdfs[0]["size"] <= 8_000_000:
        raise ValueError("invoice_attachment_ambiguous")
    body = pdfs[0]
    if "data" not in body:
        body = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg["id"], id=body["attachmentId"])
            .execute()
        )
    raw = base64.urlsafe_b64decode(body["data"])
    pages = PdfReader(io.BytesIO(raw)).pages
    if len(pages) > 20:
        raise ValueError("invoice_page_limit")
    return "\n".join(page.extract_text(extraction_mode="layout") for page in pages)


def sender_of(msg):
    headers = msg["payload"]["headers"]
    froms = [parseaddr(h["value"])[1].lower() for h in headers if h["name"].lower() == "from"]
    auth = next((h["value"] for h in headers if h["name"].lower() == "authentication-results"), "")
    if len(froms) != 1 or not auth.lstrip().lower().startswith("mx.google.com;"):
        return None
    domain = froms[0].split("@")[-1]
    if re.search(r"\bdkim=pass\b[^;]*\bheader\.i=[^;\s]*@" + re.escape(domain) + r"(?=\s|;|$)", auth, re.I):
        return froms[0]
    return None


def collect(scope, snapshot, owner, unknown, cutoff, mailbox):
    policy = scope["submissions"]
    projects = policy["projects"]
    if set(projects) != set(scope["project_ids"]):
        raise SourceIncomplete("submission_project_scope")
    builders = policy["builders"]
    data = snapshot["clients"][owner]
    known = {(r["supplier"].casefold(), r["number"].casefold()): r for r in data["contractor_invoices"]}
    historical = set()
    for row in data["hours"]:
        historical.update(re.findall(r"\b" + ISSUE + r"\b", row["id"] + " " + row["description"]))
    groups = defaultdict(list)
    conflict = set()
    superseded = set()
    handled = set()
    messages_by_key = defaultdict(set)
    pending_issues = 0
    last = cutoff.astimezone(TZ).date().isoformat()
    seen = {}
    for issue in linear_issues(scope, policy["since"]):
        ident = issue["identifier"]
        if ident in seen:
            if seen[ident] != issue:
                raise SourceIncomplete("linear_changed_during_read")
            continue
        seen[ident] = issue
        builder = (issue.get("assignee") or {}).get("id")
        project = (issue.get("project") or {}).get("id")
        if (
            builder not in builders
            or project not in projects
            or issue["state"]["type"] != "completed"
            or not issue["completedAt"]
            or instant(issue["updatedAt"]) > cutoff
            or issue["identifier"] in historical
        ):
            continue
        try:
            entry = work_from_issue(issue)
        except (ValueError, ArithmeticError, AttributeError):
            pending_issues += 1
            continue
        if entry:
            number, start, end, quantity = entry
            if end <= last:
                groups[builder, (number or issue["identifier"]).casefold()].append(
                    (project, issue["identifier"], start, end, quantity)
                )
    invoices = {}
    for mid in sorted(unknown):
        msg = mailbox.users().messages().get(userId="me", id=mid).execute()
        if int(msg["internalDate"]) / 1000 > cutoff.timestamp():
            handled.add(mid)
            continue
        sender = sender_of(msg)
        builder = next((k for k, v in builders.items() if sender in v["senders"]), None)
        if not builder:
            continue
        try:
            number, issued, lines = invoice_rows(pdf_attachment(mailbox, msg), projects)
        except (ValueError, TypeError, AttributeError, IndexError, ArithmeticError, PdfReadError):
            continue
        key = builder, number.casefold()
        if issued > last or any(row[3] > last for row in lines):
            continue
        messages_by_key[key].add(mid)
        record = (issued, sorted(lines))
        if key in invoices and invoices[key] != record:
            conflict.add(key)
        invoices[key] = record
        handled.add(mid)
    # An explicit ticket can acquire an invoice number after the initial submission.
    # Replacement requires matching quantities and work dates, even before a
    # ticket has been annotated with its invoice number.
    all_tickets = defaultdict(set)
    for key, (_, lines) in invoices.items():
        for project, ticket, start, end, quantity in lines:
            aggregate_work = [(key, row) for row in groups.get(key, [])] if not ticket else []
            if ticket:
                all_tickets[ticket].add(key)
            for other, items in groups.items():
                if other == key:
                    continue
                for pr, ident, a, b, amount in items:
                    if (ticket and ticket == ident) or (
                        not ticket and other[0] == key[0] and pr == project and start <= b and a <= end
                    ):
                        if other[0] == key[0] and other[1] == ident.casefold():
                            superseded.add(other)
                            if ticket and (pr, ident, a, b, amount) != (project, ticket, start, end, quantity):
                                conflict.add(key)
                            if not ticket:
                                aggregate_work.append((other, (pr, ident, a, b, amount)))
                        else:
                            conflict.update((key, other))
            if aggregate_work and (
                sum(row[-1] for _, row in aggregate_work) > quantity
                or any(row[0] != project or row[2] < start or row[3] > end for _, row in aggregate_work)
            ):
                conflict.add(key)
    for owners in all_tickets.values():
        if len(owners) > 1:
            conflict.update(owners)
    # Two aggregate invoice periods for the same builder/project may be revisions.
    for key, (_, lines) in invoices.items():
        for other, (_, more) in invoices.items():
            if key >= other or key[0] != other[0]:
                continue
            if any(
                p == q and a <= d and c <= b and (not t or not u)
                for p, t, a, b, _ in lines
                for q, u, c, d, _ in more
            ):
                conflict.update((key, other))
    reviewed_rows = tuple(data["hours"])
    for key in sorted(set(groups) | set(invoices)):
        builder, number = key
        supplier = builders[builder]["supplier"]
        if key in superseded:
            continue
        reviewed = known.get((supplier.casefold(), number))
        if reviewed:
            if key in invoices and (
                sum(line[-1] for line in invoices[key][1]) != reviewed["hour_units"]
                or invoices[key][0] != reviewed["date"]
                or key in conflict
            ):
                pending_issues += 1
                handled.difference_update(messages_by_key[key])
            continue
        submitted = groups.get(key, [])
        issued, rows = invoices.get(key, (None, submitted))
        if not rows:
            continue
        if issued and any(r[1] in historical for r in rows if r[1]):
            conflict.add(key)
        if issued and submitted:
            if any(r[1] for r in rows):
                if any(r not in rows for r in submitted):
                    conflict.add(key)
            else:
                project, _, start, end, total = rows[0]
                if sum(r[-1] for r in submitted) > total or any(
                    r[0] != project or r[2] < start or r[3] > end for r in submitted
                ):
                    conflict.add(key)
        # An aggregate submission overlapping a reviewed interval cannot be added safely.
        if any(
            old.get("builder_id") == builder
            and old["project_id"] == pr
            and old["work_start"] <= end
            and start <= old["work_end"]
            and (
                not ticket
                or (
                    not re.search(r"\b" + ISSUE + r"\b", old["description"])
                    and (not issued or not old.get("invoice_id"))
                )
            )
            for pr, ticket, start, end, _ in rows
            for old in reviewed_rows
        ):
            conflict.add(key)
        if key in conflict:
            pending_issues += 1
            handled.difference_update(messages_by_key[key])
            continue
        identity = "submitted-" + digest([builder, number])[:24]
        evidence = identity
        snapshot["evidence"][evidence] = {
            "reference": "Submitted invoice or Linear work " + number,
            "sha256": digest(rows),
        }
        note = "Submitted hours; exact daily timing within a work period is estimated."
        for idx, (project, ticket, start, end, quantity) in enumerate(rows):
            data["hours"].append(
                {
                    "id": identity + ":" + (ticket or str(idx)),
                    "invoice_id": identity if issued else None,
                    "builder_id": builder,
                    "project_id": project,
                    "work_start": start,
                    "work_end": end,
                    "hour_units": quantity,
                    "date_basis": "work_dates",
                    "description": ticket or projects[project] + " invoice " + number,
                    "allocation_note": note,
                    "submission_source": "invoice" if issued else "linear",
                    "evidence_ids": [evidence],
                }
            )
        if issued:
            data["contractor_invoices"].append(
                {
                    "id": identity,
                    "number": number,
                    "supplier": supplier,
                    "date": issued,
                    "hour_units": sum(r[-1] for r in rows),
                    "excluded_hour_units": 0,
                    "unresolved_hour_units": 0,
                    "allocation_note": note,
                    "evidence_ids": [evidence],
                }
            )
    return handled, pending_issues
