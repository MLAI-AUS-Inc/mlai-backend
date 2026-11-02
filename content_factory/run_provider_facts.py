"""Merge purpose-checked provider facts without replacing execution history."""
from copy import deepcopy

from django.db import transaction

from .run_backend_facts import BackendFactConflict, _metadata
from .run_ownership import R3_BACKEND_RESULT_KEYS, current_execution_ticket
from .website_run_authority import event_owned


def save_provider_result(run, proposed, *, baseline, legacy_fields):
    """Preserve current owned lifecycle, clocks and unrelated concurrent facts.

    Callers must independently validate their exact provider/source/permission
    proof. This merge grants no authority to publish, charge or issue a ticket.
    """
    if not event_owned(run):
        run.result = proposed
        run.save(update_fields=legacy_fields)
        return run
    if any(baseline.get(key) != proposed.get(key) for key in R3_BACKEND_RESULT_KEYS):
        raise BackendFactConflict("A provider fact cannot replace backend control history.")
    expected = current_execution_ticket(run)
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        if current_execution_ticket(current) != expected:
            raise BackendFactConflict("The provider observation belongs to an older execution.")
        current.result = _metadata(baseline, proposed, current.result or {})
        current.save(update_fields=["result"])
        for field in current._meta.concrete_fields:
            setattr(run, field.attname, deepcopy(getattr(current, field.attname)))
    return run
