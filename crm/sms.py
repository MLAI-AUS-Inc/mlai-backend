"""SMS bodies: MLAI's prefix and STOP footer, segment counts and cost estimates.

Pure apart from reading the cost settings. GSM-7 text fits 160 characters in
one segment and 153 per segment after that; anything outside GSM-7 switches
the whole message to UCS-2, which fits 70, then 67.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings

SMS_PREFIX = "MLAI: "
SMS_FOOTER = "Reply STOP to opt out"
GSM7 = "gsm7"
UCS2 = "ucs2"

_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Extension characters are sent as an escape plus a code, so each counts 2.
_GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")
_LIMITS = {GSM7: (160, 153), UCS2: (70, 67)}
_CENT = Decimal("0.01")


def compose_sms(body) -> str:
    """Add ``MLAI: `` and the STOP footer unless the body already has them."""

    text = str(body or "").strip()
    if not text.lower().startswith(SMS_PREFIX.strip().lower()):
        text = SMS_PREFIX + text
    if SMS_FOOTER.lower() not in text.lower():
        text = f"{text}\n\n{SMS_FOOTER}"
    return text


def sms_segments(text) -> tuple[str, int]:
    """Return ``(encoding, segments)`` for a finished SMS body."""

    text = str(text or "")
    if all(char in _GSM7_BASIC or char in _GSM7_EXTENSION for char in text):
        encoding = GSM7
        units = [2 if char in _GSM7_EXTENSION else 1 for char in text]
    else:
        encoding = UCS2
        # UTF-16 code units: characters outside the BMP (most emoji) take two.
        units = [2 if ord(char) > 0xFFFF else 1 for char in text]
    single, multipart = _LIMITS[encoding]
    total = sum(units)
    if total <= single:
        return encoding, 1 if total else 0
    segments, used = 1, 0
    for unit in units:
        # An escape pair or a surrogate pair is never split across segments.
        if used + unit > multipart:
            segments += 1
            used = 0
        used += unit
    return encoding, segments


def cost_setting(name: str) -> Decimal:
    """A decimal-string cost setting; unset or unreadable values count as 0."""

    try:
        value = Decimal(str(getattr(settings, name, "0") or "0").strip())
    except InvalidOperation:
        return Decimal("0")
    return value if value.is_finite() and value > 0 else Decimal("0")


def estimate_cost(segments, count) -> Decimal:
    """Estimated AUD for ``count`` messages of ``segments`` segments each."""

    rate = cost_setting("CRM_SMS_COST_PER_SEGMENT_AUD")
    total = rate * max(0, int(segments or 0)) * max(0, int(count or 0))
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)
