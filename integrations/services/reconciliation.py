"""
Luma -> Stripe -> Xero reconciliation service.

Builds a monthly reconciliation report for MLAI ticket sales. The pull is
**payout-driven** and **Stripe is the spine**: for every Stripe payout that
settled in the window we pull the balance transactions inside it, so each
payout's charge nets tie to the exact bank deposit.

Every Luma ticket charge carries its own attribution in Stripe metadata
(`event_api_id`, buyer `email`) and the event name in the charge `description`
(validated against live data 2026-07-01 — see the roo repo's
Luma-Stripe-Reconcile/PHASE-0.2-FINDINGS.md). So the report is assembled from
Stripe alone; Luma is only an optional enrichment and is not required here.

Output (one JSON payload):
- ``payouts`` — per payout: net (= bank deposit), gross/fee split, per-event
  breakdown, per-charge rows, refunds, and any tie-out warnings.
- ``brief`` — a base64 markdown brief for Claude Cowork to reconcile in Xero.
- ``workbook`` — an optional base64 .xlsx audit workbook (Sales detail + Payout
  summary). Omitted gracefully if openpyxl is unavailable.

Read-only. Never writes to Stripe. Buyer emails are PII — the calling view gates
this on Points Admin.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.conf import settings


STRIPE_API_BASE_URL = "https://api.stripe.com"

# Xero mapping labels used in the Cowork brief. The concrete Xero account codes
# and tracking-category options are wired up in Phase 2 (see the build plan);
# here they are human-readable placeholders in the brief.
TICKET_INCOME_ACCOUNT = "Ticket Sales"
STRIPE_FEE_ACCOUNT = "Stripe Fees"
TAX_RATE_LABEL = "account default"


class StripeConfigurationError(RuntimeError):
    """Raised when the backend is missing Stripe configuration."""


class StripeAPIError(RuntimeError):
    """Raised when Stripe rejects or fails an API request."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _dollars(cents: int) -> float:
    return round((cents or 0) / 100.0, 2)


class ReconciliationReportService:
    """Pull Stripe payouts + charges and build the reconciliation report."""

    def __init__(
        self,
        *,
        stripe_api_key: Optional[str] = None,
        stripe_api_version: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: Any = (3, 20),
    ):
        raw_key = (
            stripe_api_key
            if stripe_api_key is not None
            else getattr(settings, "STRIPE_SECRET_KEY", None)
        )
        self.stripe_api_key = str(raw_key or "").strip()
        self.stripe_api_version = str(
            stripe_api_version or getattr(settings, "STRIPE_API_VERSION", "2026-02-25.clover")
        )
        self.base_url = str(base_url or STRIPE_API_BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    # ---- public API ------------------------------------------------------

    def build_report(
        self,
        *,
        since: datetime,
        until: datetime,
        include_workbook: bool = True,
    ) -> Dict[str, Any]:
        if not self.stripe_api_key:
            raise StripeConfigurationError("STRIPE_SECRET_KEY is not configured on mlai-backend.")

        start_unix = int(since.timestamp())
        end_unix = int(until.timestamp())

        payouts = self._list(
            "/v1/payouts",
            {"arrival_date[gte]": start_unix, "arrival_date[lt]": end_unix, "status": "paid"},
        )

        payout_reports: List[Dict[str, Any]] = []
        sales_rows: List[Dict[str, Any]] = []
        unmatched: List[Dict[str, Any]] = []
        currency_totals: Dict[str, Dict[str, Any]] = {}

        for payout in sorted(payouts, key=lambda p: p.get("arrival_date") or 0):
            report, rows = self._build_payout_report(payout)
            payout_reports.append(report)
            sales_rows.extend(rows)
            for row in rows:
                if not row["event_api_id"]:
                    unmatched.append(row)

            ccy = report["currency"]
            bucket = currency_totals.setdefault(
                ccy,
                {"payouts": 0, "gross_cents": 0, "stripe_fee_cents": 0, "deposit_cents": 0},
            )
            bucket["payouts"] += 1
            bucket["gross_cents"] += report["gross_cents"]
            bucket["stripe_fee_cents"] += report["stripe_fee_cents"]
            bucket["deposit_cents"] += report["deposit_cents"]

        result: Dict[str, Any] = {
            "window": {
                "since": since.astimezone(timezone.utc).isoformat(),
                "until": until.astimezone(timezone.utc).isoformat(),
            },
            "currency_totals": {
                ccy: {
                    "payouts": b["payouts"],
                    "gross": _dollars(b["gross_cents"]),
                    "stripe_fee": _dollars(b["stripe_fee_cents"]),
                    "deposit": _dollars(b["deposit_cents"]),
                }
                for ccy, b in sorted(currency_totals.items())
            },
            "payout_count": len(payout_reports),
            "charge_count": len(sales_rows),
            "unmatched_charge_count": len(unmatched),
            "payouts": payout_reports,
        }

        brief_md = self._render_brief(payout_reports, result)
        result["brief"] = {
            "filename": self._brief_filename(since, until),
            "content_base64": base64.b64encode(brief_md.encode("utf-8")).decode("ascii"),
            "content_type": "text/markdown",
        }

        if include_workbook:
            workbook = self._render_workbook(sales_rows, payout_reports)
            if workbook is None:
                result["workbook_error"] = "openpyxl is not installed; xlsx workbook was skipped."
            else:
                result["workbook"] = {
                    "filename": self._brief_filename(since, until).replace(".md", ".xlsx"),
                    "content_base64": base64.b64encode(workbook).decode("ascii"),
                    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }

        return result

    # ---- payout assembly -------------------------------------------------

    def _build_payout_report(self, payout: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        payout_id = payout.get("id", "")
        currency = str(payout.get("currency") or "").upper()
        arrival = _unix_to_date(payout.get("arrival_date"))

        txns = self._list(
            "/v1/balance_transactions",
            {"payout": payout_id, "expand[]": "data.source"},
        )

        sales_rows: List[Dict[str, Any]] = []
        refunds: List[Dict[str, Any]] = []
        gross_cents = 0
        fee_cents = 0
        net_cents = 0
        non_payout_net = 0
        events: Dict[str, Dict[str, Any]] = {}

        for txn in txns:
            ttype = txn.get("type")
            if ttype == "payout":
                # The payout line itself (negative); skip but note for tie-out.
                continue

            non_payout_net += int(txn.get("net") or 0)

            if ttype == "charge":
                source = txn.get("source") if isinstance(txn.get("source"), dict) else {}
                metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
                event_api_id = str(metadata.get("event_api_id") or "").strip()
                event_name = (
                    str(source.get("description") or txn.get("description") or "").strip()
                    or "(no event name)"
                )
                buyer_email = str(
                    metadata.get("email")
                    or (source.get("billing_details") or {}).get("email")
                    or ""
                ).strip()

                row_gross = int(txn.get("amount") or 0)
                row_fee = int(txn.get("fee") or 0)
                row_net = int(txn.get("net") or 0)
                gross_cents += row_gross
                fee_cents += row_fee
                net_cents += row_net

                row = {
                    "charge_id": str(source.get("id") or txn.get("id") or ""),
                    "event_api_id": event_api_id,
                    "event_name": event_name,
                    "buyer_email": buyer_email,
                    "gross_cents": row_gross,
                    "stripe_fee_cents": row_fee,
                    "net_cents": row_net,
                    "currency": str(txn.get("currency") or currency).upper(),
                    "created": _unix_to_iso(source.get("created") or txn.get("created")),
                    "payout_id": payout_id,
                    "payout_arrival": arrival,
                }
                sales_rows.append(row)

                key = event_api_id or "__unknown__"
                bucket = events.setdefault(
                    key,
                    {
                        "event_api_id": event_api_id,
                        "event_name": event_name,
                        "ticket_count": 0,
                        "gross_cents": 0,
                    },
                )
                bucket["ticket_count"] += 1
                bucket["gross_cents"] += row_gross

            elif ttype in ("refund", "payment_refund"):
                source = txn.get("source") if isinstance(txn.get("source"), dict) else {}
                refunds.append(
                    {
                        "id": str(txn.get("id") or ""),
                        "amount_cents": int(txn.get("amount") or 0),
                        "net_cents": int(txn.get("net") or 0),
                        "currency": str(txn.get("currency") or currency).upper(),
                        "description": str(txn.get("description") or source.get("description") or ""),
                    }
                )
            else:
                # adjustments, disputes, stripe_fee lines, etc. — surface them.
                refunds.append(
                    {
                        "id": str(txn.get("id") or ""),
                        "type": ttype,
                        "amount_cents": int(txn.get("amount") or 0),
                        "net_cents": int(txn.get("net") or 0),
                        "currency": str(txn.get("currency") or currency).upper(),
                        "description": str(txn.get("description") or ""),
                    }
                )

        refund_net_cents = sum(int(r.get("net_cents") or 0) for r in refunds)
        payout_amount = int(payout.get("amount") or 0)

        warnings: List[str] = []
        if non_payout_net != payout_amount:
            warnings.append(
                "Tie-out mismatch: sum of transaction nets "
                f"({_dollars(non_payout_net)}) != payout amount ({_dollars(payout_amount)})."
            )
        if any(not r["event_api_id"] for r in sales_rows):
            warnings.append("Some charges have no Luma event_api_id — event must be assigned manually.")
        if refunds:
            warnings.append(f"{len(refunds)} non-charge transaction(s) (refunds/adjustments) in this payout.")

        report = {
            "payout_id": payout_id,
            "arrival_date": arrival,
            "currency": currency,
            # deposit_cents is the ACTUAL bank line (payout amount). It equals
            # charge_net_cents + refund_net_cents when the payout ties out.
            "payout_amount_cents": payout_amount,
            "deposit_cents": payout_amount,
            "gross_cents": gross_cents,
            "stripe_fee_cents": fee_cents,
            "charge_net_cents": net_cents,       # gross - Stripe fees, before refunds
            "refund_net_cents": refund_net_cents,
            "charge_count": len(sales_rows),
            "events": sorted(events.values(), key=lambda e: e["event_name"]),
            "charges": sales_rows,
            "refunds": refunds,
            "warnings": warnings,
        }
        return report, sales_rows

    # ---- Stripe HTTP -----------------------------------------------------

    def _list(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        starting_after: Optional[str] = None
        while True:
            page_params = dict(params, limit=100)
            if starting_after:
                page_params["starting_after"] = starting_after
            page = self._get(path, page_params)
            rows = page.get("data", []) if isinstance(page, dict) else []
            out.extend(rows)
            if not page.get("has_more") or not rows:
                break
            last_id = rows[-1].get("id")
            if not last_id:
                break
            starting_after = last_id
        return out

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.stripe_api_key}",
            "Stripe-Version": self.stripe_api_version,
        }
        try:
            response = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise StripeAPIError("Unable to reach Stripe.") from exc

        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403):
            raise StripeAPIError("Stripe rejected the configured API key.", status_code=status_code)
        if status_code == 429:
            raise StripeAPIError("Stripe rate-limited the reconciliation request.", status_code=status_code)

        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise StripeAPIError("Stripe returned an error.", status_code=status_code) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise StripeAPIError("Stripe returned an invalid JSON response.", status_code=status_code) from exc
        return payload if isinstance(payload, dict) else {}

    # ---- rendering -------------------------------------------------------

    @staticmethod
    def _brief_filename(since: datetime, until: datetime) -> str:
        a = since.astimezone(timezone.utc).date().isoformat()
        b = until.astimezone(timezone.utc).date().isoformat()
        return f"reconciliation-{a}-to-{b}.md"

    def _render_brief(self, payouts: List[Dict[str, Any]], summary: Dict[str, Any]) -> str:
        L: List[str] = []
        L.append("# Stripe payout reconciliation brief")
        L.append("")
        L.append("**For Claude Cowork.** Reconcile each Stripe deposit below in the Xero")
        L.append("bank feed. **Only these Stripe/Luma deposits — leave every other bank")
        L.append("line (transfers, PayID, etc.) untouched.**")
        L.append("")
        L.append("For each: open the bank line -> **Create** -> fill Who / Why, then")
        L.append("**Add details** to split by event. Each split line sets **What** (account),")
        L.append("**Event Name**, **Project Name**, **Amount**, **Tax Rate**. The split total")
        L.append("must equal the bank line to the cent. **A human clicks OK to confirm.**")
        L.append("")
        L.append("> Tax Rate is left as the income account's default — confirm it in Xero.")
        L.append("")

        L.append("## Deposits to reconcile")
        L.append("")
        L.append("| Payout | Arrived | Bank deposit | Events | Charges |")
        L.append("|---|---|---|---|---|")
        for p in payouts:
            evs = ", ".join(e["event_name"] for e in p["events"]) or "—"
            L.append(
                f"| `{p['payout_id']}` | {p['arrival_date']} | "
                f"{p['currency']} {_dollars(p['deposit_cents']):,.2f} | {evs} | {p['charge_count']} |"
            )
        if summary.get("unmatched_charge_count"):
            L.append("")
            L.append(
                f"> ⚠ {summary['unmatched_charge_count']} charge(s) have no Luma "
                "event_api_id — assign the event manually (marked below)."
            )
        L.append("")
        L.append("---")
        L.append("")

        for p in payouts:
            ccy = p["currency"]
            L.append(f"## {p['payout_id']} — {ccy} {_dollars(p['deposit_cents']):,.2f} received {p['arrival_date']}")
            L.append("")
            L.append(
                f"**Match the bank line:** Received **{ccy} {_dollars(p['deposit_cents']):,.2f}** "
                f"on/around **{p['arrival_date']}** (payer: Stripe)."
            )
            L.append("")
            L.append("- **Who:** Stripe Payments")
            events_list = ", ".join(e["event_name"] for e in p["events"]) or "—"
            L.append(f"- **Why:** Luma tickets — {events_list} — {p['charge_count']} charge(s) — payout {p['payout_id']}")
            L.append("")
            L.append("**Create → Add details (split lines):**")
            L.append("")
            L.append("| What (account) | Event Name | Project Name | Amount | Tax Rate |")
            L.append("|---|---|---|---|---|")
            for e in p["events"]:
                label = e["event_name"] if e["event_api_id"] else f"⚠ pick — {e['event_name']}"
                L.append(
                    f"| {TICKET_INCOME_ACCOUNT} | {label} | (set if used) | "
                    f"{_dollars(e['gross_cents']):,.2f} | {TAX_RATE_LABEL} |"
                )
            if p["stripe_fee_cents"]:
                L.append(
                    f"| {STRIPE_FEE_ACCOUNT} | — | — | "
                    f"-{_dollars(p['stripe_fee_cents']):,.2f} | {TAX_RATE_LABEL} |"
                )
            for r in p["refunds"]:
                desc = r.get("description") or r.get("type") or "refund/adjustment"
                L.append(
                    f"| Refund/adjustment — {desc} | — | — | "
                    f"{_dollars(r.get('net_cents', 0)):,.2f} | {TAX_RATE_LABEL} |"
                )
            L.append(
                f"| **TOTAL — must equal the bank line** | | | "
                f"**{_dollars(p['deposit_cents']):,.2f}** | |"
            )
            L.append("")
            if p["warnings"]:
                for w in p["warnings"]:
                    L.append(f"> ⚠ {w}")
                L.append("")
            L.append("<details><summary>Buyers in this payout (audit — not entered per line)</summary>")
            L.append("")
            for c in p["charges"]:
                L.append(
                    f"- {c['buyer_email'] or '(no email)'} — {c['event_name']} — "
                    f"{ccy} {_dollars(c['gross_cents']):,.2f} — {c['created']}"
                )
            L.append("")
            L.append("</details>")
            L.append("")
            L.append("> Cowork fills the form; **a human clicks OK to confirm the reconcile.**")
            L.append("")

        return "\n".join(L)

    def _render_workbook(
        self, sales_rows: List[Dict[str, Any]], payouts: List[Dict[str, Any]]
    ) -> Optional[bytes]:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            return None

        head_fill = PatternFill("solid", fgColor="1F3864")
        head_font = Font(bold=True, color="FFFFFF")

        wb = Workbook()

        def render(ws, columns, data):
            ws.append([label for _, label, _ in columns])
            for col_idx in range(1, len(columns) + 1):
                cell = ws.cell(1, col_idx)
                cell.fill, cell.font = head_fill, head_font
                cell.alignment = Alignment(horizontal="center")
            for record in data:
                ws.append([fn(record) for _, _, fn in columns])
            for col_idx, (_, label, _) in enumerate(columns, start=1):
                letter = get_column_letter(col_idx)
                width = max([len(str(label))] + [len(str(fn(r))) for _, _, fn in [columns[col_idx - 1]] for r in data]) if data else len(str(label))
                ws.column_dimensions[letter].width = min(max(width + 3, 11), 42)
            ws.freeze_panes = "A2"

        sales_cols = [
            ("created", "Date bought (UTC)", lambda r: r["created"]),
            ("event_name", "Event", lambda r: r["event_name"]),
            ("event_api_id", "Event id", lambda r: r["event_api_id"]),
            ("buyer_email", "Buyer", lambda r: r["buyer_email"]),
            ("gross", "Gross", lambda r: _dollars(r["gross_cents"])),
            ("stripe_fee", "Stripe fee", lambda r: _dollars(r["stripe_fee_cents"])),
            ("net", "Net", lambda r: _dollars(r["net_cents"])),
            ("currency", "Ccy", lambda r: r["currency"]),
            ("charge_id", "Stripe charge", lambda r: r["charge_id"]),
            ("payout_id", "Payout", lambda r: r["payout_id"]),
            ("payout_arrival", "Payout date", lambda r: r["payout_arrival"]),
        ]
        ws1 = wb.active
        ws1.title = "Sales detail"
        render(ws1, sales_cols, sales_rows)

        payout_cols = [
            ("payout_id", "Payout", lambda r: r["payout_id"]),
            ("arrival_date", "Arrival date", lambda r: r["arrival_date"]),
            ("currency", "Ccy", lambda r: r["currency"]),
            ("charge_count", "# Charges", lambda r: r["charge_count"]),
            ("gross", "Gross", lambda r: _dollars(r["gross_cents"])),
            ("stripe_fee", "Stripe fee", lambda r: _dollars(r["stripe_fee_cents"])),
            ("refunds", "Refunds/adj", lambda r: _dollars(r["refund_net_cents"])),
            ("deposit", "Bank deposit", lambda r: _dollars(r["deposit_cents"])),
            ("events", "Events", lambda r: ", ".join(e["event_name"] for e in r["events"])),
        ]
        ws2 = wb.create_sheet("Payout summary")
        render(ws2, payout_cols, payouts)

        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()


def _unix_to_date(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def _unix_to_iso(value: Any) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError, TypeError):
        return ""
