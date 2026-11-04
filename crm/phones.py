"""Phone numbers for contacts, normalised to E.164 without a new dependency."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEPARATORS = re.compile(r"[\s\-().]")


@dataclass(frozen=True)
class ContactPhone:
    e164: str
    is_au_mobile: bool


NO_PHONE = ContactPhone(e164="", is_au_mobile=False)


def normalize_contact_phone(value, default_region: str = "AU") -> ContactPhone:
    """Return the number as +E.164, or ``NO_PHONE`` when it can't be read.

    Australian numbers may be written locally (0412 345 678), without the
    trunk zero (412 345 678) or with the country code but no plus
    (61412345678). Any other country must be written with a leading + or 00.
    """

    text = _SEPARATORS.sub("", str(value or ""))
    if text.startswith("00"):
        text = "+" + text[2:]
    if text.startswith("+"):
        digits = text[1:]
    elif default_region == "AU" and text.isdigit():
        if len(text) == 10 and text.startswith("0"):
            digits = "61" + text[1:]
        elif len(text) == 11 and text.startswith("61"):
            digits = text
        elif len(text) == 9 and text.startswith("4"):
            digits = "61" + text
        else:
            return NO_PHONE
    else:
        return NO_PHONE
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return NO_PHONE
    if digits.startswith("610"):
        # +61 0412… is a common mix of local and international formats.
        digits = "61" + digits[3:]
    e164 = "+" + digits
    return ContactPhone(e164=e164, is_au_mobile=len(digits) == 11 and digits.startswith("614"))


def sms_allowed(e164: str, allowed_prefixes) -> bool:
    return bool(e164) and any(e164.startswith(prefix) for prefix in allowed_prefixes)


def sms_status_for(phone: ContactPhone, current: str) -> str:
    """The SMS state a number gives a contact; an opt-out always sticks."""

    if current == "opted_out":
        return current
    if not phone.e164:
        return "none"
    if phone.e164.startswith("+61") and not phone.is_au_mobile:
        return "invalid"
    return "active"
