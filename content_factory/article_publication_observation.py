"""Bounded positive-only recovery of an interrupted article Git push.

The existing lifecycle outbox holds the immutable intent before a provider write.
No missing ref, timeout, closed PR or elapsed deadline can authorize a refund.
"""
from datetime import timedelta
import json
from urllib.parse import quote

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .article_publication_intent import validate, digest
from .article_publication_evidence import original_scope, original_goal, mapping, publication_state, cancellation_receipt, _checked_publication
from .website_failure_contract import canonical_bytes
from .website_contract import WebsiteAuthorityError

ACTION = "publication-observe"
MAX_ATTEMPTS = 3
DEADLINE = timedelta(hours=24)


def _same_scope(run, intent, website):
    from .run_state import ARTICLE_WORKFLOWS
    from .website_operations import deletion_epoch
    scope = intent["scope"]
    try:
        goal_id = original_goal(run)
    except (ValueError, TypeError):
        return False
    generation = mapping(run.result).get("generation", intent["generation"])
    return (run.workflow in ARTICLE_WORKFLOWS and run.run_id == intent["run_id"]
        and run.organization_id == website.organization_id
        and str(run.domain).casefold() == str(website.organization.domain).casefold()
        and str(run.github_repo).casefold() == str(website.github_repo).casefold()
        and canonical_bytes(original_scope(run)) == canonical_bytes(scope) and goal_id == intent["goal_id"]
        and scope["website_connection_id"] == str(website.pk) and scope["connection_generation"] == website.generation
        and scope["repository_id"] == website.repository_id and scope["github_repo"].casefold() == website.github_repo.casefold()
        and scope["deletion_epoch"] == deletion_epoch(website)
        and all(key not in scope or scope[key] == getattr(website, key) for key in ("branch", "app_root"))
        and type(generation) is int and generation == intent["generation"])


def register_intent(website, mutation, data):
    """Called inside the existing mutation authority transaction before Git push."""
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnectionOperation
    raw = data.get("article_publication_intent_v1")
    if raw is None:
        return None
    try:
        intent = validate(raw)
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=intent["run_id"]).first()
        files = mutation.files
        if (run is None or not _same_scope(run, intent, website)
                or mutation.run_id != run.run_id or mutation.generation != website.generation
                or mutation.operation_id != f"{website.pk}:{intent['mutation_id']}"
                or mutation.base_sha != intent["base_sha"] or mutation.branch != intent["branch_name"]
                or digest(files) != intent["files_digest"] or len(files) != intent["file_count"]
                or any(item.get("kind") != "published_article" for item in files)
                or not any(item.get("after_sha256") for item in files)):
            raise ValueError("Article publication intent scope mismatch")
        operation, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key=f"article-observation:{website.pk}:{intent['intent_id']}",
            defaults={"connection": website, "generation": website.generation, "action": ACTION,
                "payload": {"intent": intent}, "next_attempt_at": timezone.now() + timedelta(minutes=1)})
        if (not created and (operation.action != ACTION or operation.connection_id != website.pk
                or canonical_bytes(operation.payload) != canonical_bytes({"intent": intent}))):
            raise ValueError("Article publication intent identity conflict")
        return intent["intent_id"]
    except (ValueError, TypeError, KeyError) as exc:
        raise WebsiteAuthorityError("invalid_mutation_ledger", "The original article publication intent could not be verified.", status=409) from exc


def _observation(website, intent):
    """Prove exact repository/ref/commit/tree/parent, using only bounded GETs."""
    from .website_connections import contract_for, authority_guard, require_unlocked_remote_call
    from .website_tokens import mint_website_token
    from integrations import http_client
    if intent["push_repository"] != {"id": website.repository_id, "full_name": website.github_repo}:
        raise ValueError("Fork publication observations require separate repository consent")
    binding = {**contract_for(website), "domain": website.organization.domain, "github_repo": website.github_repo}
    with authority_guard(binding, action="read"):
        pass
    require_unlocked_remote_call()
    credential = mint_website_token(binding, permission_mode="read", action="read")
    headers = {"Authorization": f"Bearer {credential['github_token']}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{website.github_repo}"
    try:
        repo = _provider_json(base, headers=headers)
        ref = _provider_json(f"{base}/git/ref/heads/{quote(intent['branch_name'], safe='')}", headers=headers)
        commit = _provider_json(f"{base}/git/commits/{intent['head_sha']}", headers=headers)
        if (type(repo.get("id")) is not int or repo.get("id") != website.repository_id or repo.get("full_name") != website.github_repo
                or ref.get("ref") != "refs/heads/" + intent["branch_name"]
                or mapping(ref.get("object")).get("type") != "commit"
                or mapping(ref.get("object")).get("sha") != intent["head_sha"]
                or commit.get("sha") != intent["head_sha"] or mapping(commit.get("tree")).get("sha") != intent["tree_sha"]
                or not isinstance(commit.get("parents"), list) or len(commit["parents"]) != 1
                or mapping(commit["parents"][0]).get("sha") != intent["base_sha"]):
            raise ValueError("Provider content commit does not match the original article intent")
        return {"provider": "github", "intent_id": intent["intent_id"], "repository": intent["push_repository"],
            "branch_name": intent["branch_name"], "head_sha": intent["head_sha"],
            "tree_sha": intent["tree_sha"], "base_sha": intent["base_sha"]}
    finally:
        # Revoke this observer's ephemeral read token, never mutate repository state.
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
        except Exception:
            pass


def _provider_json(url, *, headers):
    from integrations import http_client
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    response = http_client.get(url, headers=headers, timeout=(3, 10), stream=True, allow_redirects=False)
    return _response_json(response)


def _response_json(response):
    """Bound provider and private-service responses before deserialization."""
    try:
        if response.status_code != 200:
            raise ValueError("Provider observation unavailable")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("Provider observation exceeds its limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Invalid provider observation")
        return result
    finally:
        response.close()


def _send_observation(run, intent, observation):
    """Send a fact to the private ledger endpoint, granting no run-control authority."""
    from integrations import http_client
    from .website_connections import require_unlocked_remote_call
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    require_unlocked_remote_call()
    config = _content_factory_remote_config()
    if not config["enabled"]:
        raise ValueError("Publication observer is unavailable")
    response = http_client.post(f"{config['base_url']}/api/runs/{run.run_id}/publication-observation",
        headers=_content_factory_headers(), json={"intent": intent, "observation": observation},
        timeout=(3, 10), stream=True, allow_redirects=False)
    return _response_json(response)


def _locked(operation_id):
    """Use the established org → connection → operation → run lock order."""
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from .models import OrganizationContentConfig
    candidate = WebsiteConnectionOperation.objects.filter(pk=operation_id, action=ACTION).first()
    if candidate is None:
        return None
    Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
    website = WebsiteConnection.objects.select_for_update().select_related("organization").get(pk=candidate.connection_id)
    op = WebsiteConnectionOperation.objects.select_for_update().get(pk=operation_id, action=ACTION)
    try:
        intent = validate(op.payload["intent"])
    except (ValueError, TypeError, KeyError):
        return website, op, None, None
    run = ContentFactoryRun.objects.select_for_update().filter(run_id=intent["run_id"]).first()
    # Holding the connection lock serializes the original workflow's attempt
    # changes. A stale saved run must not borrow the newer operation's consent.
    try:
        original = WebsiteConnectionOperation.objects.filter(pk=intent["scope"]["operation_id"],
            connection=website, action="workflow", generation=website.generation).first()
    except (ValidationError, ValueError, TypeError):
        original = None
    if (website.state not in {"connected", "paused"}
            or not OrganizationContentConfig.objects.filter(organization_id=website.organization_id,
                website_connection=website, github_repo__iexact=website.github_repo).exists()
            or original is None or original.payload.get("run_id") != intent["run_id"]
            or type(original.payload.get("attempt")) is not int
            or original.payload["attempt"] != intent["scope"]["operation_attempt"]
            or original.state in {"deleted", "denied"}):
        run = None
    return website, op, intent, run


def _hold(op, reason, *, now):
    op.state, op.next_attempt_at = "needs_attention", None
    op.receipt = {**mapping(op.receipt), "status": "needs_reconciliation", "reason": reason, "checked_at": now.isoformat()}
    op.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
    return "needs_attention"


def advance(operation_id, *, now=None):
    """Observe only stopped uncertain work; preserve all financial obligations."""
    now = now or timezone.now()
    with transaction.atomic():
        rows = _locked(operation_id)
        if rows is None:
            return None
        website, op, intent, run = rows
        if op.state != "pending" or op.next_attempt_at and op.next_attempt_at > now:
            return None
        if run is None or not _same_scope(run, intent, website):
            return _hold(op, "original_publication_scope_unavailable", now=now)
        if publication_state(run) == "article":
            op.state, op.next_attempt_at = "completed", None
            op.receipt = {"status": "article_effect_already_recorded"}
            op.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return "completed"
        if now - op.created_at >= DEADLINE or op.attempts >= MAX_ATTEMPTS:
            return _hold(op, "publication_observation_budget_exhausted", now=now)
        try:
            stopped = cancellation_receipt(run, run.result)
        except (ValueError, TypeError):
            stopped = None
        if run.status != "cancelled" or stopped is None or stopped["outcome"] != "unknown":
            # Waiting for real worker quiescence does not consume provider attempts.
            op.next_attempt_at = now + timedelta(minutes=5)
            op.save(update_fields=["next_attempt_at", "updated_at"])
            return "pending"
        op.attempts += 1
        op.next_attempt_at = now + timedelta(minutes=5)
        op.receipt = {"status": "observing", "attempt": op.attempts}
        op.save(update_fields=["attempts", "next_attempt_at", "receipt", "updated_at"])
        claim = (op.attempts, canonical_bytes(intent), website.generation, website.configuration_version,
            website.state, digest(mapping(run.result)))
    try:
        observation = _observation(website, intent)
        # The provider read grants no mutation lease. Recheck before sending the
        # positive fact to CF, and once more before committing its acknowledgement.
        with transaction.atomic():
            current = _locked(operation_id)
            if not _current(current, claim, intent):
                return "pending"
        reply = _send_observation(run, intent, observation)
        with transaction.atomic():
            current = _locked(operation_id)
            if not _current(current, claim, intent):
                return "pending"
            website, op, intent, run = current
            if reply.get("intent_id") != intent["intent_id"]:
                raise ValueError("Publication observation acknowledgement unavailable")
            proof = _checked_publication(run, reply.get("publication_evidence_v1"))
            if (isinstance(proof, list) or proof.get("purpose") != "article" or proof.get("state") != "observed"
                    or proof.get("head_sha") != intent["head_sha"] or proof.get("branch_name") != intent["branch_name"]):
                raise ValueError("Publication observation acknowledgement mismatch")
            run.result = {**mapping(run.result), "publication_evidence_v1": proof, "reconciliation_required": True,
                "publication_observation_intent_id": intent["intent_id"]}
            run.save(update_fields=["result", "updated_at"])
            op.state, op.next_attempt_at = "completed", None
            op.receipt = {"status": "article_effect_observed", "observation": observation}
            op.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            parent = mapping(run.run_request).get("first_article_goal_operation_id")
            if parent:
                from .website_first_article_refunds import wake_settlement
                transaction.on_commit(lambda: wake_settlement(parent_id=parent,
                    organization_id=run.organization_id, child_run_id=run.run_id,
                    publication_intent_id=intent["intent_id"]), robust=True)
            return "completed"
    except Exception:
        # No provider body, token or exception text is persisted or logged.
        with transaction.atomic():
            current = _locked(operation_id)
            if current and _current(current, claim, intent):
                _, op, _, _ = current
                if op.attempts >= MAX_ATTEMPTS:
                    return _hold(op, "publication_observation_unconfirmed", now=now)
                op.receipt = {"status": "pending", "reason": "publication_observation_unconfirmed"}
                op.save(update_fields=["receipt", "updated_at"])
        return "pending"


def _current(rows, claim, intent):
    if rows is None:
        return False
    website, op, saved, run = rows
    return (op.state == "pending" and run is not None and run.status == "cancelled"
        # Any newer worker fact, including a replacement stop/publication receipt,
        # wins over this in-flight read. The next bounded pass can inspect it.
        and (op.attempts, canonical_bytes(saved), website.generation, website.configuration_version,
            website.state, digest(mapping(run.result))) == claim
        and canonical_bytes(saved) == canonical_bytes(intent) and _same_scope(run, intent, website))
