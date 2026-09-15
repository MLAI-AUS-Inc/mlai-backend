"""Convert only legacy plaintext; preserve valid ciphertext and empty values.

All credential writers must use 0039's compatibility field before a real-world
backfill. This migration does not make an older plaintext-writing binary safe.
"""

from django.db import migrations, transaction
from django.views.decorators.debug import sensitive_variables

from integrations.fields import (
    CredentialEncryptionError,
    decrypt_credential_value,
    encrypt_credential_value,
)

BATCH_SIZE = 100
TOKEN_FIELDS = ("github_token_encrypted", "github_refresh_token_encrypted")
ENCRYPTED_PREFIXES = ("mlai-enc:", "gAAAA")


@sensitive_variables()
def converted_value(value):
    if value in (None, ""):
        return value
    if value.startswith(ENCRYPTED_PREFIXES):
        # Validate first; never reinterpret damaged ciphertext as a token.
        decrypt_credential_value(value)
        return value
    return encrypt_credential_value(value)


def layout(apps, connection):
    model = apps.get_model("content_factory", "OrganizationContentConfig")
    quote = connection.ops.quote_name
    return (
        quote(model._meta.db_table),
        quote(model._meta.pk.column),
        [quote(model._meta.get_field(name).column) for name in TOKEN_FIELDS],
    )


@sensitive_variables()
def read_batch(cursor, table, pk, columns, after, *, lock=False):
    where = f" WHERE {pk} > %s" if after is not None else ""
    params = [after, BATCH_SIZE] if after is not None else [BATCH_SIZE]
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"SELECT {pk}, {', '.join(columns)} FROM {table}{where} "
        f"ORDER BY {pk} LIMIT %s{suffix}", params,
    )
    return cursor.fetchall()


@sensitive_variables()
def verify_encrypted(apps, connection):
    table, pk, columns = layout(apps, connection)
    after = None
    while True:
        with connection.cursor() as cursor:
            rows = read_batch(cursor, table, pk, columns, after)
        if not rows:
            return
        for row in rows:
            for value in row[1:]:
                if value in (None, ""):
                    continue
                if not value.startswith(ENCRYPTED_PREFIXES):
                    raise CredentialEncryptionError("GitHub credential backfill verification failed.")
                decrypt_credential_value(value)
        after = rows[-1][0]


@sensitive_variables()
def backfill_github_credentials(apps, schema_editor):
    connection = schema_editor.connection
    table, pk, columns = layout(apps, connection)
    after = None
    try:
        while True:
            # The outer migration is non-atomic. Each batch commits on its own,
            # but every credential pair is read and updated under its row lock.
            with transaction.atomic(using=connection.alias):
                with connection.cursor() as cursor:
                    rows = read_batch(
                        cursor, table, pk, columns, after,
                        lock=connection.features.has_select_for_update,
                    )
                    if not rows:
                        break
                    for row in rows:
                        values = tuple(converted_value(value) for value in row[1:])
                        if values != tuple(row[1:]):
                            cursor.execute(
                                f"UPDATE {table} SET {columns[0]} = %s, {columns[1]} = %s "
                                f"WHERE {pk} = %s", [*values, row[0]],
                            )
            after = rows[-1][0]
        verify_encrypted(apps, connection)
    except CredentialEncryptionError:
        # Avoid leaking ciphertext/key identifiers through a management-command
        # traceback. Already committed batches are safe to revisit on restart.
        raise CredentialEncryptionError(
            "GitHub credential backfill could not validate encrypted values. "
            "Check the credential keyring and stored envelope integrity before retrying."
        ) from None


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("content_factory", "0039_encrypt_github_credentials")]
    operations = [
        # No reverse function: rollback must never restore plaintext at rest.
        migrations.RunPython(backfill_github_credentials, atomic=False),
    ]
