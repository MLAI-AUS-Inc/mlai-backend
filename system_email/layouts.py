"""Shared presentation for callers that already build their own email copy."""

from urllib.parse import urlsplit

from django.template.loader import render_to_string
from django.utils.safestring import SafeData


def _web_url(value: str) -> str:
    try:
        url = urlsplit(value)
    except ValueError:
        return ""
    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
        return ""
    return value


def render_transactional_email(
    *,
    title: str,
    paragraphs: tuple[str, ...] = (),
    action_label: str = "",
    action_url: str = "",
    preheader: str = "",
    preferences_url: str = "",
    unsubscribe_url: str = "",
    content_html: SafeData | str = "",
    secondary_links: tuple[tuple[str, str], ...] = (),
) -> str:
    """Render the MLAI layout. The caller supplies its existing plain-text part.

    Pass plain copy as paragraphs. Only pass existing, escaped HTML fragments
    (or an independently rendered template) as Django safe content.
    """
    return render_to_string(
        "system_email/transactional.html",
        {
            "subject": title,
            "paragraphs": paragraphs,
            "action_label": action_label,
            "action_url": _web_url(action_url),
            "preheader": preheader,
            "preferences_url": _web_url(preferences_url),
            "unsubscribe_url": _web_url(unsubscribe_url),
            "content_html": content_html,
            "secondary_links": [(label, safe_url) for label, url in secondary_links if (safe_url := _web_url(url))],
        },
    )
