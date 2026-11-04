"""Roo top-up GST policy and signed payment validation (no database access)."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

GST_POLICY_VERSION = "roo-points-au-gst-inclusive-2026-10-13"


def inclusive_gst_snapshot(amount_cents: int, currency: str, tax_rate_id: str = "") -> dict:
    if type(amount_cents) is not int or amount_cents <= 0 or currency.lower() != "aud":
        raise ValueError("Roo Points GST requires a positive AUD purchase amount")
    gst_cents = int((Decimal(amount_cents) / 11).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "policy_version": GST_POLICY_VERSION,
        "name": "GST",
        "country": "AU",
        "percentage": "10",
        "inclusive": True,
        "currency": "aud",
        "total_cents": amount_cents,
        "subtotal_cents": amount_cents - gst_cents,
        "amount_cents": gst_cents,
        "stripe_tax_rate_id": tax_rate_id,
    }


def validate_gst_rate(rate: dict, expected_id: str) -> None:
    if not isinstance(rate, dict):
        raise RuntimeError("Roo Points GST rate could not be verified with Stripe")
    try:
        percentage = Decimal(str(rate.get("percentage")))
    except (InvalidOperation, ValueError):
        percentage = None
    if (
        rate.get("id") != expected_id
        or rate.get("active") is not True
        or rate.get("inclusive") is not True
        or rate.get("country") != "AU"
        or percentage != Decimal("10")
        or rate.get("tax_type") != "gst"
        or rate.get("display_name") != "GST"
    ):
        raise RuntimeError("Roo Points requires an active inclusive Australian GST rate of 10%")


def validate_gst_payment(session: dict, purchase) -> dict | None:
    """Validate new taxed sessions; never invent tax evidence for legacy sessions."""
    tax = (purchase.metadata or {}).get("tax")
    if not tax:
        if (session.get("metadata") or {}).get("gst_policy_version"):
            raise ValueError("Roo Points GST snapshot is missing for a taxed Stripe session")
        return None
    expected = inclusive_gst_snapshot(purchase.amount_cents, purchase.currency, tax.get("stripe_tax_rate_id", ""))
    if tax != expected or not tax["stripe_tax_rate_id"]:
        raise ValueError("Roo Points purchase tax snapshot is inconsistent")
    amount_tax = (session.get("total_details") or {}).get("amount_tax")
    if (
        type(session.get("amount_total")) is not int
        or session["amount_total"] != purchase.amount_cents
        or session.get("currency") != purchase.currency.lower()
        or type(amount_tax) is not int
        or amount_tax != tax["amount_cents"]
        or (session.get("metadata") or {}).get("gst_policy_version") != tax["policy_version"]
    ):
        raise ValueError("Stripe payment amount, currency or GST does not match the Roo Points purchase")
    payment_intent = session.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    return {
        "checkout_session_id": session["id"],
        "payment_intent_id": payment_intent,
        "currency": session["currency"],
        "total_cents": session["amount_total"],
        "gst_cents": amount_tax,
    }
