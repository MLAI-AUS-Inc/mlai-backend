"""One writer for the existing globally unique financial-record identity.

Until tenant-owned external accounts have an approved schema, a record cannot
move between connections as a side effect of a sync. Conflicts fail explicitly.
"""

from django.db import transaction

from integrations.models import ExternalFinancialRecord


class FinancialRecordOwnershipError(ValueError):
    pass


@transaction.atomic
def upsert_financial_record(
    *, external_record_id, defaults, connection=None, provider=None,
    external_account_id=None, record_type=None,
):
    values = dict(defaults)
    connection = connection if connection is not None else values.get("connection")
    if connection is None or connection.pk is None:
        raise ValueError("A persisted connection is required for financial records.")
    provider = provider or values.get("provider") or connection.provider
    if provider != connection.provider:
        raise FinancialRecordOwnershipError("Financial record provider does not match its connection.")
    if external_account_id is None:
        external_account_id = values.get("external_account_id", connection.external_account_id)
    if not external_record_id or not external_account_id:
        raise ValueError("Financial records require upstream account and record identifiers.")
    identity = {
        "provider": provider,
        "external_account_id": external_account_id,
        "external_record_id": external_record_id,
    }
    owner = {
        "connection_id": connection.pk,
        "organization_id": connection.organization_id,
        "user_id": connection.user_id,
    }
    for field in ("connection", "organization", "user"):
        values.pop(field, None)
    values.update(owner)
    values.update(identity)
    if record_type is not None:
        values["record_type"] = record_type
    account = values.get("financial_account")
    if account is not None and account.connection_id != connection.pk:
        raise FinancialRecordOwnershipError("Financial account does not match its connection.")

    # The existing unique constraint arbitrates concurrent first inserts.
    # get_or_create preserves select_for_update on its conflict/re-read path.
    record, created = ExternalFinancialRecord.objects.select_for_update().get_or_create(
        **identity, defaults={**values, **identity},
    )
    if created:
        return record, True
    if any(getattr(record, field) != value for field, value in owner.items()):
        raise FinancialRecordOwnershipError(
            "This financial record belongs to another connection. "
            "Shared-account history requires an explicit ownership transfer."
        )
    if values.get("record_type") != record.record_type:
        raise FinancialRecordOwnershipError("Financial record type conflicts with existing history.")
    for field, value in values.items():
        setattr(record, field, value)
    record.save()
    return record, False
