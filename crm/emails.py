from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from core.models import User


def normalize_contact_email(value) -> str:
    """Return the lowercase contact key, or "" when the value is not an email."""

    email = User.objects.normalize_email(value)
    if not email or len(email) > 254:
        return ""
    try:
        validate_email(email)
    except ValidationError:
        return ""
    return email
