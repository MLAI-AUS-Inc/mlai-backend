"""Organization research domains; syntax validation grants no website authority."""
import re

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_TLD = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)\Z", re.ASCII)


def validate_refresh_domain(value):
    """Return a bounded ASCII domain, never extracting a host from arbitrary input."""
    if not isinstance(value, str):
        raise ValueError("A valid organization domain is required.")
    domain = value.strip().lower()
    labels = domain.split(".")
    if (not domain.isascii() or len(domain) > 253 or len(labels) < 2
            or any(not _LABEL.fullmatch(label) for label in labels)
            or not _TLD.fullmatch(labels[-1])):
        raise ValueError("A valid organization domain is required.")
    return domain

