"""One owner acceptance persists an encrypted initial key and original hold."""

from copy import deepcopy
import hmac
from uuid import uuid4

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from . import cms_initial_authority as authority
from . import cms_initial_contract as grants
from . import cms_onboarding_offer as offers
from . import cms_onboarding_plan as plans
from . import cms_goal_budget as budget
from . import cms_financial_holds as holds
from . import cms_first_article_subject as subjects
from . import cms_setup_contract as setup_contract
from .cms_access_contract import wait_epoch
from .cms_owner_connection import _request_id
from .cms_connection_contract import seal_credential, open_credential, CmsContractError
from .channel_artifact_contract import canonical_bytes, digest
from .website_publishing_channels import saved_record, scope_for
from .website_contract import WebsiteAuthorityError

FIELDS = {"client_request_id", "offer", "credential_origin_confirmed", "username", "secret", *setup_contract.ANSWER_FIELDS}


def _conflict():
    return WebsiteAuthorityError("operation_key_conflict", "Read the original initial CMS setup before starting another.")


def _credential(binding, username, secret):
    try:
        packet = seal_credential(binding, username=username, secret=secret)
        return packet, open_credential(packet, binding=binding)
    except (CmsContractError, ImproperlyConfigured):
        raise WebsiteAuthorityError("missing_or_expired_credentials", "Check the CMS application or integration credential format.", status=422) from None


def _same(left, right):
    return (hmac.compare_digest(left.username.encode(), right.username.encode()) and
        hmac.compare_digest(left.secret.encode(), right.secret.encode()))


def _grant(job, *, plan, packet, request_id, epoch, now):
    from .website_models import WebsiteConnectionOperation
    identifier = str(uuid4())
    grant = grants.grant(operation_id=identifier, plan=plan, request_id=request_id, access_epoch=epoch, proposal_digest=digest(packet), now=now)
    row, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"cms-initial-grant:{job.pk}:{request_id}", defaults={
        "id": identifier, "connection": job.connection, "generation": job.generation, "action": authority.GRANT_ACTION,
        "state": "pending", "payload": {"schema_version": 1, "grant": grant, "sealed_credential": packet}, "receipt": {"status": "accepted"}})
    if not created: raise _conflict()
    return row


def start(context, config, *, user, data, now=None):
    """Accept actual choices and original price once, before any provider I/O."""
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    if not authority.enabled(context.organization.pk):
        raise WebsiteAuthorityError("publishing_adapter_required", "Initial CMS setup is unavailable.", status=404)
    if not isinstance(data, dict) or set(data) != FIELDS or data["credential_origin_confirmed"] is not True:
        raise offers._deny()
    request_id, now = _request_id(data), now or timezone.now()
    try: original = offers.validate(data["offer"])
    except (ValueError, TypeError, KeyError, AttributeError): raise offers._deny() from None
    original_key = f"cms-onboarding:{context.organization.pk}:{context.company.pk}:{request_id}"
    request_digest = digest({key: item for key, item in data.items() if key not in {"username", "secret"}})
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        prior = WebsiteConnectionOperation.objects.select_for_update().filter(idempotency_key=original_key, action=authority.ACTION).first()
        if prior is not None:
            value = authority.scope(prior, user=user, pending=prior.state != "completed", now=now)
            if value.context.company.pk != context.company.pk or str(value.config.pk) != str(config.pk) or value.plan["offer"] != original or prior.receipt.get("owner_request_digest") != request_digest:
                raise _conflict()
            _packet, proposed = _credential(value.binding, data["username"], data["secret"])
            if prior.state == "completed":
                from .cms_connection_authority import current_credential
                saved = current_credential(value.context, value.config, value.connection, user=value.user)[1]
                if value.connection.capabilities.get("_cms_connect_request_id") != request_id or not _same(saved, proposed): raise _conflict()
            else:
                if prior.state not in authority.ACTIVE: raise _conflict()
                if not _same(authority.proposal(value, now=now).credential, proposed): raise _conflict()
            return value.root
        # The display offer can be old but must still be unexpired at the first
        # acceptance. Its fresh equivalent differs only in display timestamps.
        offers.validate(original, now=now)
        fresh = offers.for_context(context, config, user=user, data={"platform": original["platform"],
            "public_origin": original["public_origin"], "api_origin": original["api_origin"],
            "channel_scope": original["scope"], "channel_revision": original["channel_revision"]}, now=now)
        stable = lambda value: {key: item for key, item in value.items() if key not in {"quoted_at", "expires_at", "digest"}}
        if fresh is None or canonical_bytes(stable(fresh)) != canonical_bytes(stable(original)): raise offers._deny()
        binding = {"schema_version": 1, "channel": "cms", "platform": original["platform"], "company_id": str(context.company.pk),
            "organization_id": str(context.organization.pk), "owner_user_id": str(user.pk), "connection_id": str(uuid4()), "generation": 1,
            "public_origin": original["public_origin"], "api_origin": original["api_origin"]}
        stage_id, root_id, private_id, intent_id = (str(uuid4()) for _ in range(4))
        plan = plans.prepare(original, binding=binding, operation_id=stage_id, root_operation_id=root_id,
            credential_operation_id=private_id, request_id=request_id, user_id=user.pk,
            answers={key: data[key] for key in setup_contract.ANSWER_FIELDS}, now=now)
        packet, _proposed = _credential(binding, data["username"], data["secret"])
        connection = WebsiteConnection.objects.create(id=binding["connection_id"], organization=context.organization, authorized_by=user,
            state="paused", github_repo="", repository_id=None, installation_id="", branch="", app_root="", site_url=binding["public_origin"],
            capabilities={"_cms_binding": binding, "_cms_credential_operation_id": private_id, "_cms_initial_operation_id": stage_id}, blockers=[])
        WebsiteConnectionOperation.objects.create(id=private_id, connection=connection, generation=1, action="channel-credential", state="pending",
            idempotency_key=f"cms-key:{connection.pk}:1", payload={"schema_version": 1, "binding": binding}, receipt={})
        intent = plan["native_intent"]
        root = WebsiteConnectionOperation.objects.create(id=root_id, connection=connection, generation=1, action="cms-setup", state="pending",
            next_attempt_at=now, idempotency_key=f"cms-setup:{connection.pk}:1:{request_id}",
            payload={"schema_version": 1, "binding": binding, "credential_operation_id": private_id, "company_id": binding["company_id"],
                "requested_by_user_id": str(user.pk), "intent_operation_id": intent_id, "intent_digest": intent["digest"]},
            receipt={"status": "initial_connecting", "current_step": "verify_cms", "native_budget": budget.initial_at(intent["accepted_at"])})
        WebsiteConnectionOperation.objects.create(id=intent_id, connection=connection, generation=1, action="cms-setup-intent", state="completed",
            idempotency_key="cms-setup-intent:" + root_id, payload=intent, receipt={"status": "accepted"})
        job = WebsiteConnectionOperation.objects.create(id=stage_id, connection=connection, generation=1, action=authority.ACTION, state="pending",
            next_attempt_at=now, idempotency_key=original_key, payload={"schema_version": 1, "plan": plan, "grant_operation_id": str(uuid4()), "failures": 0},
            receipt={"status": "verification_pending", "owner_request_digest": request_digest})
        proposed = _grant(job, plan=plan, packet=packet, request_id=request_id, epoch=None, now=now)
        job.payload["grant_operation_id"] = str(proposed.pk); job.save(update_fields=["payload", "updated_at"])
        record = saved_record(config.article_system)
        config.github_repo = ""
        config.website_connection = connection
        config.github_installation_id, config.github_token_encrypted, config.github_refresh_token_encrypted = "", "", ""
        config.publish_targets, config.default_publish_target_id = [], None
        config.auto_publish, config.daily_discovery_enabled = False, False
        config.article_system = {**config.article_system, "publishing_channel": {**deepcopy(record), "scope": scope_for(context, config), "revision": record["revision"] + 1}}
        config.save(update_fields=["github_repo", "website_connection", "github_installation_id", "github_token_encrypted", "github_refresh_token_encrypted",
            "publish_targets", "default_publish_target_id", "auto_publish", "daily_discovery_enabled", "article_system", "updated_at"])
        if not authority.enabled(context.organization.pk): raise authority._deny()
        holds.reserve(root, subjects.from_setup_intent(intent, expected_binding=binding), context=context, connection=connection, user=user, now=now)
        authority.scope(job, user=user, now=now)
        if not authority.enabled(context.organization.pk): raise authority._deny()
        return root


def restore(context, config, *, user, data, now=None):
    """Accept only a new proposal for the actual original initial key wait."""
    from .website_models import WebsiteConnectionOperation
    from organizations.models import Organization
    from . import website_goal_budget as waits
    required = {"operation_id", "client_request_id", "access_epoch", "credential_origin_confirmed", "username", "secret"}
    if not authority.enabled(context.organization.pk) or not isinstance(data, dict) or set(data) != required or data["credential_origin_confirmed"] is not True:
        raise authority._deny()
    now, request_id = now or timezone.now(), _request_id(data)
    try: grants.uuid(data["operation_id"])
    except ValueError: raise authority._deny() from None
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        job = WebsiteConnectionOperation.objects.select_for_update().filter(action=authority.ACTION, connection_id=config.website_connection_id,
            payload__plan__root_operation_id=data["operation_id"]).first()
        if job is None: raise authority._deny()
        value = authority.scope(job, user=user, pending=job.state != "completed", now=now)
        if str(value.context.company.pk) != str(context.company.pk) or str(value.config.pk) != str(config.pk): raise authority._deny()
        if job.state == "completed":
            selected = authority.proposal(value, require_key=False, now=now)
            _packet, proposed = _credential(value.binding, data["username"], data["secret"])
            from .cms_connection_authority import current_credential
            if (selected.grant["request_id"] != request_id or selected.grant["access_epoch"] != data["access_epoch"] or
                    value.connection.capabilities.get("_cms_connect_request_id") != request_id or
                    not _same(current_credential(value.context, value.config, value.connection, user=value.user)[1], proposed)):
                raise _conflict()
            return value.root
        if str(value.context.company.pk) != str(context.company.pk) or wait_epoch(value.root) != data["access_epoch"] or data["access_epoch"] is None:
            raise authority._deny()
        packet, proposed = _credential(value.binding, data["username"], data["secret"])
        existing = WebsiteConnectionOperation.objects.filter(idempotency_key=f"cms-initial-grant:{job.pk}:{request_id}").first()
        if existing is not None:
            selected = authority.proposal(value, now=now)
            if selected.row.pk != existing.pk or selected.grant["access_epoch"] != data["access_epoch"] or not _same(selected.credential, proposed): raise _conflict()
            return value.root
        if job.state != "needs_user": raise _conflict()
        grant = _grant(job, plan=value.plan, packet=packet, request_id=request_id, epoch=data["access_epoch"], now=now)
        job.payload = {key: item for key, item in job.payload.items() if key not in {"lease_id", "lease_grant_id"}}
        job.payload["grant_operation_id"] = str(grant.pk)
        job.state, job.next_attempt_at = "pending", now
        job.receipt = {**job.receipt, "status": "verification_pending"}
        job.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])
        selected = waits.update_owner_wait(value.budget, waiting=False, now=now)
        value.root.receipt = {**value.root.receipt, "native_budget": selected}
        value.root.next_attempt_at = now
        value.root.save(update_fields=["receipt", "next_attempt_at", "updated_at"])
        authority.proposal(authority.scope(job, user=user, now=now), now=now)
        return value.root
