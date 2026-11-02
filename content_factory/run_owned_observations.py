"""Retire owned legacy snapshots and callbacks before their side-effect guards."""
from copy import deepcopy

from rest_framework.response import Response

from .run_ownership import (WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, RunOwnershipConflict,
    admitted_run_for_key, bind_admitted_run, current_execution_ticket, owner_receipt, prepare_owned_callback,
    require_event_execution, require_event_identity)


def refresh_owned_run(run):
    """Read typed history and verified artifacts, never the supplied snapshot."""
    from django.db import connection
    from .run_reconciliation import authoritative_run, reconcile_authoritative_run
    from .run_verified_delivery import project_verified_delivery
    from .website_contract import WebsiteAuthorityError
    import requests
    if not authoritative_run(run):
        return None
    if not connection.in_atomic_block:
        reconcile_authoritative_run(run.run_id, refresh=True)
        try:
            from .run_topic_delivery import topic_kind, project_verified_topics
            (project_verified_topics if topic_kind(run) else project_verified_delivery)(run.run_id)
        except (ValueError, TypeError, KeyError, requests.RequestException, WebsiteAuthorityError):
            # Typed lifecycle remains visible when independent artifact proof
            # is pending; an old snapshot never supplies that missing proof.
            pass
    run.refresh_from_db()
    return run


def maybe_receive_owned_observation(payload, *, run_id=None, expected_kind=None):
    """Return None only for genuinely unmarked legacy input.

    Worker state is discarded. The bounded typed evidence reader supplies any
    actual delivery facts, independently of these lifecycle-shaped hints.
    """
    from workflow_runs.models import ContentFactoryRun
    from .run_reconciliation import authoritative_run
    from .run_verified_delivery import project_verified_delivery
    from .setup_delivery_contract import DeliveryContractError
    if not isinstance(payload, dict):
        return None
    wrapped = "delivery_v1" in payload
    nested = payload.get("delivery_v1") if wrapped else payload
    request = payload.get("run_request") if isinstance(payload.get("run_request"), dict) else {}
    wire = payload.get(WIRE_REQUEST_KEY, request.get(WIRE_REQUEST_KEY))
    key = wire.get("dispatch_key") if isinstance(wire, dict) else payload.get("client_request_id", request.get("client_request_id"))
    identifier = run_id or (nested.get("run_id") if isinstance(nested, dict) else None) or payload.get("job_id")
    run = admitted_run_for_key(key)
    if run is None and identifier:
        run = ContentFactoryRun.objects.filter(run_id=identifier).first()
    if not authoritative_run(run):
        if owner_receipt(run) is not None and not wrapped:
            return None
        if wrapped or wire is not None:
            return Response({"error": "run_admission_unavailable", "retryable": True}, status=409)
        return None
    try:
        if wrapped:
            from .setup_delivery_contract import validate_envelope
            if (set(payload) != {"schema_version", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, "delivery_v1"}
                    or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1):
                raise RunOwnershipConflict("The owned delivery wrapper is invalid.")
            envelope = validate_envelope(nested, expected_kind=expected_kind, path_run_id=run_id)
            require_event_identity(run, payload)
            ticket = require_event_execution(run, payload, generation=envelope["execution"]["generation"])
            if (envelope["scope"]["domain"] != run.domain or envelope["scope"]["github_repo"] != run.github_repo
                    or any(envelope["scope"].get(field) != value for field, value in ticket["scope"].items()
                           if field in envelope["scope"])):
                raise RunOwnershipConflict("The delivery scope differs from its creation ticket.")
            if envelope["run_id"] != run.run_id:
                if ticket != current_execution_ticket(run):
                    raise RunOwnershipConflict("A retained ticket cannot bind a new run identity.")
                run = bind_admitted_run(run, envelope["run_id"])
        else:
            envelope = None
            normalized = deepcopy(payload)
            run = prepare_owned_callback(normalized, identifier or run.run_id)
            if not authoritative_run(run):
                raise RunOwnershipConflict("The existing owner is unavailable.")
        from .run_topic_delivery import topic_kind, project_verified_topics
        outcome = (project_verified_topics if topic_kind(run) else project_verified_delivery)(run.run_id)
    except (RunOwnershipConflict, DeliveryContractError, TypeError, KeyError, ValueError):
        return Response({"error": "run_observation_pending", "retryable": True, "run_id": run.run_id}, status=409)
    except Exception as exc:
        # Provider transport/errors keep evidence pending. No legacy handler
        # or in-transaction notification/refund is a fallback for owned work.
        import requests
        from .website_contract import WebsiteAuthorityError
        if isinstance(exc, (requests.RequestException, WebsiteAuthorityError)):
            return Response({"error": "run_observation_pending", "retryable": True, "run_id": run.run_id}, status=409)
        raise
    if envelope is not None:
        from .setup_delivery_receiver import acknowledgement
        return Response(acknowledgement(envelope, "observation_only"), status=200)
    return Response(outcome, status=200)
