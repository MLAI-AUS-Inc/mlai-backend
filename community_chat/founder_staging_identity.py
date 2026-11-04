"""Stored identity checks for the dedicated fixture; no effect on old profiles."""
import secrets

from cryptography.fernet import InvalidToken
from django.contrib.auth import get_user_model

from .founder_staging_email import fixture_scope


class InvalidFixtureIdentity(ValueError):
    """A stored record is outside the reserved browser/recipient namespace."""


def _refuse():
    raise InvalidFixtureIdentity("Founder fixture identity is invalid")


def require_recipient(email, user):
    """A reserved alias must never grant access to another primary account."""
    scope = fixture_scope()
    if scope is None:
        return
    try:
        scope.validate_email(email)
    except (TypeError, ValueError):
        _refuse()
    if user is not None and user.email != email:
        _refuse()


def precheck_recipient(email):
    """Refuse a foreign primary before the ordinary request throttle writes."""
    if fixture_scope() is not None:
        require_recipient(email, get_user_model().objects.resolve_email(email))


def require_context(*, client_id, origin, platform):
    """All stored capabilities must belong to the single web origin/client."""
    scope = fixture_scope()
    if scope is not None and (client_id != scope.client or origin != scope.origin or platform != "web"):
        _refuse()
    return scope


def require_challenge(challenge, *, client_id, installation_id):
    """Check before attempts, consumption or signup; caller retains its locks."""
    if fixture_scope() is None:
        return
    if challenge is None or challenge.client_id != client_id or str(challenge.installation_id) != str(installation_id):
        _refuse()
    require_context(client_id=challenge.client_id, origin=challenge.origin, platform=challenge.platform)
    if challenge.user_id is not None:
        email = challenge.user.email
        require_recipient(email, challenge.user)
    else:
        from .email_delivery import decrypt_signup_email
        try:
            email = decrypt_signup_email(challenge.encrypted_signup_email)
        except (InvalidToken, ValueError, UnicodeError):
            _refuse()
        precheck_recipient(email)
    from .email_codes import email_digest
    if not secrets.compare_digest(email_digest(email), challenge.email_digest):
        _refuse()


def precheck_verification(*, challenge_id, client_id, installation_id):
    """Read-only refusal before verify throttling; consume rechecks under lock."""
    if fixture_scope() is None:
        return
    from .models import CommunityChatEmailCodeChallenge
    challenge = CommunityChatEmailCodeChallenge.objects.select_related("user").filter(pk=challenge_id).first()
    require_challenge(challenge, client_id=client_id, installation_id=installation_id)


def require_session(session):
    """Call before last-used, token rotation or revocation in their own locks."""
    if fixture_scope() is None:
        return
    if session is None:
        _refuse()
    require_context(client_id=session.client_id, origin=session.origin, platform=session.platform)
    user = getattr(session, "user", None)
    if user is None:
        _refuse()
    require_recipient(user.email, user)
