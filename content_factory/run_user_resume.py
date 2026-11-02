"""Current-user continuation of one admitted Run, without changing its inputs."""
from .run_ownership import (
    BUDGET_KEY, SCOPE_FIELDS, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    owner_receipt, require_event_execution, wire_identity,
)
from .website_contract import evidence_digest


def user_resume_payload(*, run, user, command_key, payload):
    """Add one durable execution grant after the caller's original action gates.

    A repository operation may already have issued its exact resume ticket.
    Otherwise an explicit stable command key identifies the single user
    intention, including after the HTTP acknowledgement is lost.
    """
    receipt = owner_receipt(run)
    if receipt is None or receipt["authority"].get("event_protocol") != 1:
        return payload
    from founder_tools import startup_team
    from django.contrib.auth import get_user_model
    from .run_execution_control import (
        ExecutionControlDenied, append_execution_ticket, current_execution_request, require_current_principal,
    )
    from workflow_runs.models import ContentFactoryRun
    with startup_team._locked(run.organization) as (_, roster):
        locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        if (user is None or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists()
                or roster and roster.get("frozen")
                or locked.status not in {"queued", "running", "failed", "blocked"}
                or locked.approval_state not in {"approved", "not_required"}):
            raise ExecutionControlDenied("execution_requires_review")
        if not (startup_team._owner(locked.organization) == user.pk
                or locked.workflow in startup_team.ARTICLE_WORKFLOWS and startup_team.enabled()
                and "write_articles" in startup_team._rights(user, locked.organization, roster)):
            raise ExecutionControlDenied("execution_permission_changed")
        require_current_principal(locked)
        ticket = payload.get(WIRE_EXECUTION_KEY)
        if ticket is not None:
            if not isinstance(ticket, dict):
                raise ExecutionControlDenied("execution_resume_ticket_unavailable")
            require_event_execution(locked, payload, generation=ticket.get("generation"), current=True)
            # The caller must forward the operation controller's freshly
            # authorized ticket, rather than append another generation.
            from .run_execution_control import CONTROL_KEY
            grants = (locked.result or {}).get(CONTROL_KEY, {}).get("grants", {})
            if not isinstance(grants, dict) or not any(isinstance(grant, dict) and grant.get("execution_id") == ticket["execution_id"]
                       and grant.get("command", {}).get("reason") == "operation_resume"
                       for grant in grants.values()):
                raise ExecutionControlDenied("execution_resume_ticket_unavailable")
        else:
            if type(command_key) is not str or not command_key.strip() or command_key != command_key.strip() or len(command_key) > 255:
                raise ExecutionControlDenied("execution_command_key_required")
            key = evidence_digest({"run_pk": locked.pk, "reason": "user_resume", "command_key": command_key})
            ticket = append_execution_ticket(locked, control_key=key, reason="user_resume")
        current = current_execution_request(locked)
        result = dict(payload)
        for field in (*SCOPE_FIELDS, "configuration_revision", "github_installation_id"):
            result.pop(field, None)
            if field in current:
                result[field] = current[field]
        result.update({WIRE_REQUEST_KEY: wire_identity(owner_receipt(locked)), WIRE_EXECUTION_KEY: ticket})
        result.pop(BUDGET_KEY, None)
        if BUDGET_KEY in current:
            result[BUDGET_KEY] = current[BUDGET_KEY]
        run.result = locked.result
        return result
