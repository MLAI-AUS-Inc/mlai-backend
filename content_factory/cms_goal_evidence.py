"""Read native goal proofs from owning private validators, without effects."""

from copy import deepcopy

from . import cms_goal_contract as goals
from . import cms_publication_policy as policies
from . import cms_artifact_review as reviews
from . import cms_staged_review as stages
from . import cms_article_approval as approvals
from . import cms_article_publication as publications
from .channel_artifacts import authorize_payload, enabled
from .cms_article_source import current_article, original_billing
from .cms_capture import ACTION as CAPTURE_ACTION, _progress
from .cms_inventory_contract import validate_inventory
from .cms_owner_connection import _owner
from .cms_connection_authority import current_credential
from .website_contract import WebsiteAuthorityError


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The original native goal, article or consent changed.")


def current_scope(context, config, *, user):
    """Reload only the selected native company/owner and its private setup policy."""
    if not enabled():
        raise _deny()
    for value in (context.organization, context.profile, context.company, user, config): value.refresh_from_db()
    _owner(context, config, user)
    saved = policies.current_policy(context, config, user=user)
    if saved is None:
        raise _deny()
    binding, _credential = current_credential(context, config, config.website_connection, user=user)
    if saved["policy"]["binding"] != binding:
        raise _deny()
    value = goals.scope(saved["policy"]["binding"], policy_operation_id=saved["operation_id"], policy_digest=saved["policy"]["digest"])
    return value, saved


def collect(goal, context, config, *, user, run=None, artifact_operation_id=None, capture_operation_id=None, now):
    """Collect facts for the controller's original reservation; never schedule.

    The controller owns the immutable artifact-operation reference. This reader
    cannot select a different paid run from worker/owner evidence. A reference
    or copied receipt never supplies its proof: each owning private validator
    rechecks exact source, policy, billing and media/public lineage.
    """
    from .website_models import WebsiteConnectionOperation
    expected, saved_policy = current_scope(context, config, user=user)
    value = goals.validate_goal(goal, expected)
    binding = expected["binding"]
    rows = WebsiteConnectionOperation.objects.filter(connection_id=binding["connection_id"], generation=binding["generation"])
    policy_row = rows.filter(pk=saved_policy["operation_id"], action=policies.ACTION, state="completed").first()
    if policy_row is None:
        raise _deny()
    proofs, references = {}, {}
    original_source = None
    def put(name, subject, at, *, row=None):
        proofs[name] = goals.make_proof(name, expected_scope=expected, original_source=original_source,
            subject_digest=goals.digest(subject), at=at)
        if row is not None:
            references[name] = str(row.pk)
    # The original native-policy controller verified the authenticated native
    # author and permissions. Re-reading its current private choice cannot grant
    # a provider lease, but records that completed setup observation.
    put("access", {"binding": binding, "credential_operation_id": saved_policy["policy"]["credential_operation_id"]},
        policy_row.updated_at, row=policy_row)
    put("integrated", saved_policy["policy"], policy_row.updated_at, row=policy_row)
    captures = rows.filter(action=CAPTURE_ACTION, state="completed")
    if capture_operation_id is not None:
        captures = captures.filter(pk=capture_operation_id)
    capture = captures.order_by("-created_at", "-pk").first()
    if capture is not None:
        payload = capture.payload
        if (not isinstance(payload, dict) or payload.get("binding") != binding or
                payload.get("credential_operation_id") != saved_policy["policy"]["credential_operation_id"]):
            raise _deny()
        inventory = validate_inventory(payload.get("inventory"), binding=binding)
        if capture.receipt != _progress(inventory):
            raise _deny()
        if inventory["coverage"] == "complete":
            put("understood", {"inventory_digest": inventory["digest"], "coverage": "complete"}, capture.updated_at, row=capture)
    if run is None:
        if value["source"] is not None:
            raise _deny()
        return proofs, None, references
    run.refresh_from_db()
    admitted = authorize_payload(run.run_request)
    original_billing(run)
    if (admitted.scope.binding != binding or not isinstance(artifact_operation_id, str) or
            admitted.envelope["artifact_operation_id"] != artifact_operation_id or
            admitted.operation.payload["policy"].get("cms_publication_policy") !=
                {"operation_id": saved_policy["operation_id"], "digest": saved_policy["policy"]["digest"]} or
            str(run.organization_id) != binding["organization_id"] or
            run.workflow not in {"article_generation", "direct_generate", "confirmed_topic"} or run.github_repo or
            not isinstance(run.result, dict) or type(run.result.get("generation")) is not int):
        raise _deny()
    original_source = goals.source({"run_id": run.run_id, "run_generation": run.result["generation"], "envelope_digest": admitted.envelope["digest"]})
    prior = value["source"]
    if prior is not None and (prior["run_id"] != original_source["run_id"] or prior["envelope_digest"] != original_source["envelope_digest"] or
            prior["run_generation"] > original_source["run_generation"]):
        raise _deny()
    review = rows.filter(action=reviews.ACTION, state="completed", payload__source_run_id=run.run_id,
        payload__source_generation=original_source["run_generation"]).order_by("-created_at", "-pk").first()
    if review is None:
        return proofs, original_source, references
    article = current_article(review)
    if article.run.run_id != run.run_id or article.handoff["envelope"] != admitted.envelope:
        raise _deny()
    put("first_draft", {"handoff_digest": article.handoff["digest"], "billing_digest": article.billing["digest"]}, review.updated_at, row=review)
    stage = rows.filter(action=stages.ACTION, state="completed", payload__handoff_digest=article.handoff["digest"]).first()
    if stage is None:
        return proofs, original_source, references
    _article, staged, _media = stages.current_stage(stage, fresh_media=False, allow_media_recheck=True)
    put("previewed", {"revision": staged["revision"], "preview_sha256": staged["preview_sha256"]}, stage.updated_at, row=stage)
    approval = rows.filter(action=approvals.ACTION, state="completed", payload__stage_operation_id=str(stage.pk)).first()
    if approval is None:
        return proofs, original_source, references
    _article, _staged, _policy, receipt = approvals.current_approval(approval, fresh_media=False)
    put("approved", {"approval_digest": receipt["digest"], "staged_revision": staged["revision"]}, approval.updated_at, row=approval)
    publication = rows.filter(action=publications.ACTION, state="completed", payload__approval_operation_id=str(approval.pk)).first()
    if publication is not None:
        _article, _intent, _post_state, observation = publications.current_publication(publication, now=now)
        observed_at = goals._time(observation["checked_at"])
        for name in ("live", "first_article_live"):
            put(name, {"public_proof_digest": observation["digest"], "approval_digest": receipt["digest"]}, observed_at, row=publication)
    return deepcopy(proofs), original_source, references
