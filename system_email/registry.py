"""Every transactional message MLAI sends, keyed by a stable message key.

A message key names one email and its templates. The key is also what the
``SYSTEM_EMAIL_RESEND_MESSAGES`` switch lists, so it must never be renamed
once a deployment routes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AUTH = "auth"
NOTIFICATION = "notification"


@dataclass(frozen=True)
class MessageSpec:
    """How one message is rendered and classified.

    ``prerendered`` specs take subject/html/text from the caller, which already
    builds them in Python; the others render Django templates from context.
    ``unsubscribe`` specs are optional mail: they carry ``List-Unsubscribe``
    headers (the caller's one-click URL when it has one, plus the mailto).
    """

    key: str
    category: str
    subject: str = ""
    html_template: str = ""
    text_template: str = ""
    required_context: tuple[str, ...] = field(default_factory=tuple)
    prerendered: bool = False
    unsubscribe: bool = False


_SPECS = (
    MessageSpec(
        key="chat_sign_in_code",
        category=AUTH,
        subject="Your MLAI Chat code: {{ verification_code }}",
        html_template="system_email/chat_sign_in_code.html",
        text_template="system_email/chat_sign_in_code.txt",
        required_context=("verification_code", "expires_minutes"),
    ),
    MessageSpec(
        key="magic_link",
        category=AUTH,
        subject="Your sign-in link for {{ app_name }}",
        html_template="system_email/magic_link.html",
        text_template="system_email/magic_link.txt",
        required_context=("magic_link", "app_name"),
    ),
    MessageSpec(
        key="password_reset",
        category=AUTH,
        subject="Set or reset your MLAI password",
        html_template="system_email/password_reset.html",
        text_template="system_email/password_reset.txt",
        required_context=("reset_link",),
    ),
    MessageSpec(key="notification_channel_verify", category=AUTH, prerendered=True),
    MessageSpec(key="article_review_ready", category=NOTIFICATION, prerendered=True, unsubscribe=True),
    MessageSpec(key="article_content_ready", category=NOTIFICATION, prerendered=True, unsubscribe=True),
    MessageSpec(key="article_report_ready", category=NOTIFICATION, prerendered=True, unsubscribe=True),
    MessageSpec(
        key="monthly_update_7d",
        category=NOTIFICATION,
        subject="A quick heads-up about your coworking discount",
        html_template="system_email/monthly_update_7d.html",
        text_template="system_email/monthly_update_7d.txt",
        required_context=("startups",),
        unsubscribe=True,
    ),
    MessageSpec(
        key="monthly_update_1d",
        category=NOTIFICATION,
        subject="Your coworking discount is due to renew",
        html_template="system_email/monthly_update_1d.html",
        text_template="system_email/monthly_update_1d.txt",
        required_context=("startups",),
        unsubscribe=True,
    ),
)

MESSAGES: dict[str, MessageSpec] = {spec.key: spec for spec in _SPECS}


class UnknownMessageError(KeyError):
    """Raised for a message key that is not registered."""


def get_spec(message_key: str) -> MessageSpec:
    """Return the spec for ``message_key`` or raise ``UnknownMessageError``."""
    try:
        return MESSAGES[message_key]
    except KeyError as exc:
        raise UnknownMessageError(message_key) from exc
