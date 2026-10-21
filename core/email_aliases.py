"""Shared primary/alternate email namespace for operator-managed identities."""

import hashlib

from django.db import IntegrityError, connections


def lock_email_namespace(email, *, using='default'):
    """Serialize cross-table claims, including concurrent signup and alias writes.

    Call inside transaction.atomic. SQLite serializes writes itself; PostgreSQL
    needs a shared lock because two independent unique indexes cannot protect
    the primary/alias namespace together.
    """
    connection = connections[using]
    if connection.vendor == 'postgresql':
        key = int.from_bytes(hashlib.sha256(('mlai-email:' + email).encode()).digest()[:8], 'big', signed=True)
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])


def guard_primary_email(user, *, using='default'):
    from .models import UserEmailAlias

    lock_email_namespace(user.email, using=using)
    if UserEmailAlias.objects.using(using).filter(email__iexact=user.email).exists():
        raise IntegrityError('Email is already reserved as an alternate address.')


def guard_alias_email(alias, *, using='default'):
    from .models import User

    lock_email_namespace(alias.email, using=using)
    if User.objects.using(using).filter(email__iexact=alias.email).exists():
        raise IntegrityError('Email is already reserved as a primary address.')
    if alias.email.endswith('@slack.placeholder.com') and alias.verified_at is not None:
        raise IntegrityError('A synthetic placeholder is not a verified mailbox.')
