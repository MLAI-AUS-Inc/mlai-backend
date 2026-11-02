"""Durable current-user denial intent; typed producer events alone stop execution."""
from copy import deepcopy
from django.db import transaction
from django.utils import timezone
from .run_ownership import (USER_STOP_RESULT_KEY, RunOwnershipConflict, current_execution_ticket,
    owner_receipt, wire_identity)
from .website_contract import evidence_digest


def record_user_denial(run, *, user, actor_id):
    from founder_tools import startup_team
    from founder_tools.services import user_may_use_organization
    from integrations.services.article_generation import _validate_authenticated_content_factory_actor
    from workflow_runs.models import ContentFactoryRun
    if user is None or not user.is_active:
        raise RunOwnershipConflict("A current account is required to deny this run.")
    _validate_authenticated_content_factory_actor(user=user, actor_id=actor_id)
    with startup_team._locked(run.organization):
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        receipt = owner_receipt(current)
        if (receipt is None or receipt["authority"].get("event_protocol") != 1
                or current.organization_id != run.organization_id
                or not user_may_use_organization(user, current.organization)):
            raise RunOwnershipConflict("Current startup permission is required to deny this run.")
        ticket = current_execution_ticket(current)
        state = deepcopy((current.result or {}).get(USER_STOP_RESULT_KEY, {"schema_version": 1,
            "admission_digest": evidence_digest(wire_identity(receipt)), "commands": {}}))
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or state.get("admission_digest") != evidence_digest(wire_identity(receipt))
                or not isinstance(state.get("commands"), dict) or len(state["commands"]) >= 1024):
            raise RunOwnershipConflict("The saved denial history could not be verified.")
        identity = {"action": "deny", "user_id": str(user.pk), "actor_id": actor_id,
            "execution_id": ticket["execution_id"], "execution_sha256": evidence_digest(ticket)}
        key = evidence_digest({"run_pk": current.pk, **identity})
        previous = state["commands"].get(key)
        if previous is not None:
            if not isinstance(previous, dict) or any(previous.get(field) != value for field, value in identity.items()):
                raise RunOwnershipConflict("The saved denial command changed.")
        else:
            state["commands"][key] = {**identity, "requested_at": timezone.now().isoformat()}
            current.result = {**current.result, USER_STOP_RESULT_KEY: state}
            ContentFactoryRun.objects.filter(pk=current.pk).update(result=current.result)
        run.result = deepcopy(current.result)
        return deepcopy(state["commands"][key])
