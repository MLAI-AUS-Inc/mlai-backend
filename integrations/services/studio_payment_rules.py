"""Independent, deterministic builder/work/customer matching. No provider writes."""
from collections import defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re


class ReviewError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def quantity(hours):
    """Canonical seconds/credit fractions. Accept printed six-decimal hour values."""
    try:
        value = Decimal(str(hours)) * 3600
        rounded = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if not value.is_finite() or value <= 0 or abs(value - rounded) > Decimal(".0036"):
            raise ReviewError("invalid_quantity")
        return int(rounded)
    except (InvalidOperation, TypeError):
        raise ReviewError("invalid_quantity") from None


def cents(amount):
    try:
        value = Decimal(str(amount)) * 100
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise ReviewError("invalid_money")
        return int(value)
    except (InvalidOperation, TypeError):
        raise ReviewError("invalid_money") from None


def normal_number(number):
    value = str(number).strip().casefold()
    if not re.fullmatch(r"[\w][\w ./-]{0,149}", value):
        raise ReviewError("invoice_number_required")
    return value


def _days(row):
    start, end = (date.fromisoformat(row[key]) for key in ("start", "end"))
    if start > end:
        raise ReviewError("invalid_work_dates")
    return start, end


def claim_fingerprint(claim):
    return digest({key: claim[key] for key in ("builder", "number", "currency", "cents", "lines")})


def review(claims, work, funding, ledger, *, opening_verified):
    """Whole invoices pass together. Trial allocations prevent intra-batch reuse."""
    work = deepcopy(work)
    funding = deepcopy(funding)
    work_by_key = {row["key"]: row for row in work}
    funds_by_key = {row["key"]: row for row in funding}
    if len(work_by_key) != len(work) or len(funds_by_key) != len(funding):
        raise ReviewError("duplicate_source_identity")
    used_work, used_hours, used_money = defaultdict(int), defaultdict(int), defaultdict(int)
    paid, held, compromised = {}, set(), set()
    for reservation in ledger.values():
        if reservation["status"] not in {"reserved", "unknown", "paid"}:
            continue
        for invoice in reservation["invoices"]:
            if reservation["status"] == "paid":
                paid[invoice] = reservation.get("claim_hash")
            else:
                held.add(invoice)
        for allocation in reservation["allocations"]:
            used_work[allocation["work"]] += allocation["units"]
            used_hours[allocation["fund"]] += allocation["units"]
            used_money[allocation["fund"]] += allocation["cents"]
    # A reversal cannot silently make previously consumed money available again.
    for key in set(used_hours) | set(used_money):
        fund = funds_by_key.get(key)
        if not fund or used_hours[key] > fund["units"] or used_money[key] > fund["cents"]:
            compromised.add(fund["pool"] if fund else "*")
    results, all_allocations, seen_invoices = [], [], set()
    for claim in claims:
        identity = claim["builder"] + ":" + normal_number(claim["number"])
        errors, allocations = [], []
        if identity in seen_invoices:
            errors.append({"code": "duplicate_invoice"})
        seen_invoices.add(identity)
        if identity in paid:
            if paid[identity] == claim_fingerprint(claim):
                results.append({"invoice": identity, "number": claim["number"], "status": "already_paid", "lines": []})
                continue
            errors.append({"code": "paid_invoice_revision_needs_review"})
        if identity in held:
            errors.append({"code": "payment_in_progress"})
        if not opening_verified:
            errors.append({"code": "coverage_unknown"})
        trial_work, trial_hours, trial_money = used_work.copy(), used_hours.copy(), used_money.copy()
        line_results = []
        for line in claim["lines"]:
            start, end = _days(line)
            requested = line["units"]
            if type(requested) is not int or requested <= 0 or type(line["cents"]) is not int or line["cents"] < 0:
                raise ReviewError("invalid_claim")
            candidates = [row for row in work if row["builder"] == claim["builder"]
                          and row["project"] == line["project"] and row["basis"] == line["basis"]
                          and row["start"] >= start.isoformat() and row["end"] <= end.isoformat()
                          and (not line.get("issue") or row["issue"] == line["issue"])]
            entry = {"issue": line.get("issue"), "requested_units": requested, "code": "matched",
                     "work": [{"issue": r["issue"], "approved_units": r["units"],
                               "previously_claimed_units": trial_work[r["key"]]} for r in candidates]}
            if not candidates:
                entry["code"] = "approved_work_missing"
            elif not line.get("issue") and sum(r["units"] - trial_work[r["key"]] for r in candidates) != requested:
                entry["code"] = "period_allocation_needed"
            elif sum(r["units"] - trial_work[r["key"]] for r in candidates) < requested:
                entry["code"] = "work_already_claimed_or_hours_mismatch"
            remaining, cost_left = requested, line["cents"]
            for item in sorted(candidates, key=lambda r: r["key"]):
                if entry["code"] != "matched" or not remaining:
                    break
                take = min(remaining, max(0, item["units"] - trial_work[item["key"]]))
                if not take:
                    continue
                eligible = [f for f in funding if f["pool"] == item["pool"] and f["basis"] == item["basis"]
                            and f["currency"] == claim["currency"] and f["start"] <= item["start"]
                            and f["end"] >= item["end"]]
                if item["pool"] in compromised or "*" in compromised:
                    entry["code"] = "customer_credit_changed"
                    break
                part_cost = cost_left if take == remaining else line["cents"] * take // requested
                # Allocate units and their proportional cost together to one or more pools.
                left, money = take, part_cost
                for fund in sorted(eligible, key=lambda r: (r["end"], r["key"])):
                    available = max(0, fund["units"] - trial_hours[fund["key"]])
                    amount = min(left, available)
                    if not amount:
                        continue
                    price = money if amount == left else part_cost * amount // take
                    if fund["cents"] - trial_money[fund["key"]] < price:
                        budget = max(0, fund["cents"] - trial_money[fund["key"]])
                        amount = min(amount, budget * take // max(part_cost, 1))
                        if not amount:
                            continue
                        price = money if amount == left else part_cost * amount // take
                        if price > budget:
                            continue
                    allocations.append({"invoice": identity, "work": item["key"], "fund": fund["key"], "units": amount, "cents": price})
                    trial_hours[fund["key"]] += amount
                    trial_money[fund["key"]] += price
                    trial_work[item["key"]] += amount
                    left -= amount
                    money -= price
                    if not left:
                        break
                if left:
                    entry.update(code="customer_coverage_missing", uncovered_units=left)
                    break
                remaining -= take
                cost_left -= part_cost
            line_results.append(entry)
        if sum(line["cents"] for line in claim["lines"]) != claim["cents"]:
            errors.append({"code": "invoice_total_mismatch"})
        ready = not errors and all(row["code"] == "matched" for row in line_results)
        results.append({"invoice": identity, "number": claim["number"], "source": claim.get("source", ""),
                        "status": "ready" if ready else "needs_review",
                        "cents": claim["cents"], "currency": claim["currency"], "lines": line_results, "errors": errors})
        if ready:
            used_work, used_hours, used_money = trial_work, trial_hours, trial_money
            all_allocations.extend(allocations)
    return {"invoices": results, "allocations": all_allocations,
            "ready_cents": sum(r.get("cents", 0) for r in results if r["status"] == "ready")}
