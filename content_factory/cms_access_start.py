"""Accept an encrypted owner access request before any provider verification."""

from copy import deepcopy
import hmac
from uuid import uuid4

from django.db import transaction
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from . import cms_access_authority as authority
from . import cms_access_contract as contract
from . import cms_goal_budget as budget
from . import website_goal_budget as shared_budget
from . import cms_credential_renewal as renewal
from .cms_owner_connection import _request_id, _existing
from .cms_owner_access import for_operation
from .cms_connection_contract import seal_credential, open_credential, CmsContractError
from .channel_artifact_contract import canonical_bytes, digest
from .website_contract import WebsiteAuthorityError
from .website_publishing_channels import saved_record

FIELDS = {"company_id", "website_connection_id", "connection_generation", "configuration_revision", "operation_id", "access_epoch",
    "client_request_id", "platform", "public_origin", "api_origin", "channel_scope", "channel_revision", "credential_origin_confirmed", "username", "secret"}


def _conflict():
    return WebsiteAuthorityError("operation_key_conflict", "Read the original CMS access request before starting another.")


def latest(root, *, epoch=None):
    """Select only this exact original root's private access attempt."""
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.filter(action=authority.ACTION, connection_id=root.connection_id, generation=root.generation,
        payload__intent__root_operation_id=str(root.pk))
    if epoch is not None: rows = rows.filter(payload__intent__access_epoch=epoch)
    return rows.order_by("-created_at", "-pk").first()


def start(context, config, *, user, data):
    """Persist one closed encrypted proposal; never contact the CMS or charge."""
    if not authority.enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS access renewal is unavailable.", status=404)
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise authority._deny()
    request_id = _request_id(data)
    try: contract._uuid(data["operation_id"])
    except ValueError: raise authority._deny() from None
    request_digest = digest({key: item for key, item in data.items() if key not in {"secret", "username"}})
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        config.refresh_from_db()
        root = WebsiteConnectionOperation.objects.select_for_update().filter(pk=data["operation_id"], action__in={"cms-goal", "cms-setup"},
            connection_id=config.website_connection_id, payload__company_id=str(context.company.pk)).first()
        value = authority.root_current(root, user=user, waiting=False)
        if str(value.context.company.pk) != str(context.company.pk) or str(value.context.organization.pk) != str(context.organization.pk):
            raise authority._deny()
        binding = value.actual_binding
        original_key = f"cms-access:{binding['connection_id']}:{binding['generation']}:{root.pk}:{request_id}"
        prior = WebsiteConnectionOperation.objects.filter(idempotency_key=original_key, action=authority.ACTION).first()
        try:
            proposed = open_credential(seal_credential(binding, username=data["username"], secret=data["secret"]), binding=binding)
        except (CmsContractError, ImproperlyConfigured):
            raise WebsiteAuthorityError("missing_or_expired_credentials", "Check the application or integration credential format.", status=422) from None
        if prior is not None:
            intent = authority.historical(prior)
            if intent["request_digest"] != request_digest:
                raise _conflict()
            if prior.state == "completed":
                if _existing(value.context, value.config, value.user, request_id, binding, proposed) is None:
                    raise _conflict()
                return prior
            if prior.state not in authority.ACTIVE:
                raise _conflict()
            current = authority.current(prior, user=user)
            if (not hmac.compare_digest(current.credential.username.encode(), proposed.username.encode()) or
                    not hmac.compare_digest(current.credential.secret.encode(), proposed.secret.encode())):
                raise _conflict()
            return prior
        form = for_operation(value.context, value.config, user=value.user, operation_id=str(root.pk))["cmsAccess"]
        if form is None:
            raise authority._deny()
        expected = {"company_id": form["companyId"], "website_connection_id": form["connectionId"],
            "connection_generation": form["connectionGeneration"], "configuration_revision": form["configurationRevision"],
            "operation_id": form["operationId"], "access_epoch": form["accessEpoch"], "client_request_id": request_id,
            "platform": form["platform"], "public_origin": form["publicOrigin"], "api_origin": form["apiOrigin"],
            "channel_scope": form["channelScope"], "channel_revision": form["channelRevision"], "credential_origin_confirmed": True}
        if canonical_bytes({key: data[key] for key in expected}) != canonical_bytes(expected):
            raise authority._deny()
        previous = latest(root, epoch=form["accessEpoch"])
        if previous is not None and previous.state in authority.ACTIVE:
            from .cms_access_controller import _expire
            _expire(previous, now=timezone.now())
        if previous is not None and previous.state in authority.ACTIVE:
            raise _conflict()
        public_key = f"cms-connect:{binding['organization_id']}:{binding['company_id']}:{request_id}"
        if WebsiteConnectionOperation.objects.filter(idempotency_key=public_key).exists():
            raise _conflict()
        selected = renewal.candidate(value.context, value.config, user=value.user, proposal=binding)
        if selected is None: raise authority._deny()
        now, identifier = timezone.now(), str(uuid4())
        packet = seal_credential(binding, username=proposed.username, secret=proposed.secret)
        intent = contract.make_intent(operation_id=identifier, root_operation_id=str(root.pk), root_action=root.action, binding=binding,
            configuration_revision=selected.configuration_revision, credential_operation_id=selected.credential_operation_id,
            credential_digest=selected.credential_digest, proposal_digest=digest(packet), principal_id=selected.principal_id, request_id=request_id,
            channel_choice_digest=digest(saved_record(value.config.article_system)), access_epoch=form["accessEpoch"], request_digest=request_digest, now=now)
        selected_budget = budget.reserve_access_usage(root.receipt.get("native_budget"), started_at=value.started_at,
            operation_id=str(root.pk), access_operation_id=identifier, now=now)
        selected_budget = shared_budget.update_owner_wait(selected_budget, waiting=False, now=now)
        job, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=authority.key(intent), defaults={"id": identifier,
            "connection": root.connection, "generation": root.generation, "action": authority.ACTION, "state": "pending", "next_attempt_at": now,
            "payload": {"schema_version": 1, "intent": intent, "sealed_credential": packet, "failures": 0}, "receipt": {"status": "verification_pending"}})
        if not created: raise _conflict()
        root.receipt = {**deepcopy(root.receipt), "native_budget": selected_budget}
        root.next_attempt_at = now
        root.save(update_fields=["receipt", "next_attempt_at", "updated_at"])
        authority.current(job, user=user, now=now)
        return job
