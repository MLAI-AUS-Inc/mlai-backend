"""One owner acceptance reserves original setup intent and money before discovery."""

from copy import deepcopy
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_setup_authority as authority
from . import cms_setup_billing as billing
from . import cms_setup_quote as quotes
from . import cms_setup_contract as contracts
from . import cms_goal_authority as native
from . import cms_goal_budget as budget
from .cms_capture import _consent as current_connection
from .cms_owner_connection import _request_id
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError


def _deny():
    return WebsiteAuthorityError("website_prepare_blocked", "Review the current native setup choices and first-article price.", status=422)


def start(context, config, *, user, data, now=None):
    """Persist accepted real choices and one original hold; perform no provider I/O."""
    if not authority.enabled(context.organization.pk):
        raise WebsiteAuthorityError("publishing_adapter_required", "Native article setup is not available.", status=404)
    if not isinstance(data, dict):
        raise _deny()
    request_id, now = _request_id(data), now or timezone.now()
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        for item in (context.organization, context.profile, context.company, user, config): item.refresh_from_db()
        binding = current_connection(context, config, user, data)
        WebsiteConnection.objects.select_for_update().get(pk=binding["connection_id"])
        connection_data = contract_for(config.website_connection)
        required = {"company_id", "website_connection_id", "connection_generation", "client_request_id", "setup_quote_digest", *contracts.ANSWER_FIELDS}
        allowed = {*required, *connection_data}
        if (not required <= set(data) or set(data) - allowed or data["company_id"] != str(context.company.pk) or
                type(data["connection_generation"]) is not int or any(data[key] != value for key, value in connection_data.items() if key in data)):
            raise _deny()
        answers = {key: deepcopy(data[key]) for key in contracts.ANSWER_FIELDS}
        if (not isinstance(answers["review_policy"], str) or answers["review_policy"] not in {"auto", "review"} or type(answers["expected_cost_points"]) is not int or
                not 0 <= answers["expected_cost_points"] < 10**9 or any(answers[key] is not True
                    for key in ("publication_confirmed", "native_defaults_confirmed", "billing_confirmed"))):
            raise _deny()
        key = f"cms-setup:{binding['connection_id']}:{binding['generation']}:{request_id}"
        prior = WebsiteConnectionOperation.objects.filter(idempotency_key=key).first()
        latest = config.website_connection.operations.filter(action__in=[authority.ACTION, native.ACTION],
            generation=binding["generation"], payload__company_id=binding["company_id"]).order_by("-created_at", "-pk").first()
        selected = prior or latest
        if selected is not None:
            if selected.action == native.ACTION:
                native.current(selected, user=user, states={selected.state})
                from .cms_goal_billing import historical_reservation
                historical_reservation(selected)
                child = WebsiteConnectionOperation.objects.filter(idempotency_key="cms-setup-intent:" + str(selected.pk),
                    action=authority.INTENT_ACTION, connection_id=selected.connection_id, generation=selected.generation, state="completed").first()
                try:
                    original = contracts.validate_intent(child.payload, expected_binding=binding) if child is not None else None
                except contracts.CmsSetupContractError:
                    raise _deny() from None
            else:
                original = authority.current(selected, user=user, states={selected.state}).intent
            if (original is None or original["operation_id"] != str(selected.pk) or original["answers"] != answers or
                    original["quote"]["digest"] != data["setup_quote_digest"] or
                    prior is not None and latest is not None and prior.pk != latest.pk):
                raise WebsiteAuthorityError("operation_key_conflict", "The original native setup request has different saved choices.")
            if selected.action == authority.ACTION:
                billing.historical_reservation(selected)
            return selected
        quote = quotes.for_context(context, config, user=user, expected_binding=binding)
        if quote is None or quote["digest"] != data["setup_quote_digest"]:
            raise _deny()
        identifier, private_id = str(uuid4()), str(uuid4())
        try:
            intent = contracts.make_intent(quote, operation_id=identifier, request_id=request_id, binding=binding,
                credential_operation_id=config.website_connection.capabilities["_cms_credential_operation_id"],
                user_id=user.pk, answers=answers, now=now)
        except contracts.CmsSetupContractError:
            raise _deny() from None
        operation, _created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
            "id": identifier, "connection": config.website_connection, "generation": binding["generation"], "action": authority.ACTION,
            "state": "pending", "next_attempt_at": now, "payload": {"schema_version": 1, "binding": deepcopy(binding),
                "credential_operation_id": intent["credential_operation_id"], "company_id": binding["company_id"],
                "requested_by_user_id": str(user.pk), "intent_operation_id": private_id, "intent_digest": intent["digest"]},
            "receipt": {"status": "starting", "current_step": "discover_native", "native_budget": budget.initial_at(intent["accepted_at"])}})
        WebsiteConnectionOperation.objects.get_or_create(idempotency_key="cms-setup-intent:" + identifier, defaults={
            "id": private_id, "connection": config.website_connection, "generation": binding["generation"],
            "action": authority.INTENT_ACTION, "state": "completed", "payload": intent, "receipt": {"status": "accepted"}})
        billing.reserve(operation, user=user, now=now)
        return operation


def promote(dispatch, *, now=None):
    """Turn the same held operation into a real native goal only after real policy.

    No replacement operation, consent time, price, payment key or model request
    is minted here. The private original setup intent remains a separate record.
    """
    if not isinstance(dispatch, authority.CmsSetupDispatch):
        raise authority._deny()
    from .website_models import WebsiteConnectionOperation
    from organizations.models import Organization
    from . import cms_goal_contract as goals
    from . import cms_goal_consent as consent
    from . import cms_goal_billing as native_billing
    from .cms_goal_evidence import current_scope
    from .cms_goal_start import original_payload
    now = now or timezone.now()
    candidate = WebsiteConnectionOperation.objects.filter(pk=dispatch.operation_id, action=authority.ACTION).first()
    if candidate is None:
        raise authority._deny()
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=dispatch.operation_id, action=authority.ACTION).first()
        if operation is None:
            raise authority._deny()
        value = authority.current(operation, lease_id=dispatch.lease_id)
        original = billing.require_held(operation)
        selected = budget.validate_at(operation.receipt.get("native_budget"), started_at=value.intent["accepted_at"],
            operation_id=str(operation.pk), now=now)
        from .website_goal_budget import exhaustion
        if exhaustion(selected, now=now, action="setup") is not None:
            raise WebsiteAuthorityError("website_operation_changed", "The original native setup budget is exhausted.")
        expected, policy = current_scope(value.context, value.config, user=value.user)
        if policy["policy"]["review_policy"] != value.intent["answers"]["review_policy"]:
            raise authority._deny()
        accepted_at = goals._time(value.intent["accepted_at"])
        goal = goals.new_goal(str(operation.pk), expected, now=accepted_at)
        choice = consent.seal(goal, user_id=value.user.pk, review_policy=value.intent["answers"]["review_policy"],
            expected_points=original["cost_points"], confirmed=True, now=accepted_at)
        operation.action, operation.state, operation.next_attempt_at = native.ACTION, "pending", now
        operation.payload = original_payload(value.context, binding=value.binding, policy=policy, user=value.user, choice=choice)
        operation.receipt = {"goal": goal, "native_budget": selected, "status": "starting", "current_step": "capture_existing",
            "first_article_billing": deepcopy(original)}
        operation.save(update_fields=["action", "payload", "receipt", "state", "next_attempt_at", "updated_at"])
        # This verifies the exact original held receipt under real goal/policy
        # authority; it never reprices or debits a second time.
        native_billing.reserve(operation, user=value.user, now=now)
        return operation
