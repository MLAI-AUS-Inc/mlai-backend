"""Owned parent commands never pass through legacy lifecycle projections."""
from rest_framework.response import Response

from .run_ownership import (RunOwnershipConflict, require_event_execution, require_event_identity,
    user_stop_intent)
from .run_reconciliation import authoritative_run


def require_accepted_approval(run, acknowledgement, *, actor_id, user, expected_identity):
    """A parent-only current acknowledgement records consent, not CF lifecycle."""
    from .article_publish_approval import article_review_identity, article_review_identity_is_complete
    from .publish_reviewed_draft import may_publish
    from integrations.services.article_generation import ArticleGenerationError, _validate_authenticated_content_factory_actor
    from django.contrib.auth import get_user_model
    from .run_execution_control import require_unsettled_execution
    require_unsettled_execution(run)
    user = get_user_model().objects.filter(pk=getattr(user, "pk", None), is_active=True).first()
    if (not isinstance(acknowledgement, dict) or acknowledgement.get("action") != "approve"
            or acknowledgement.get("accepted") is not True or acknowledgement.get("run_id") != run.run_id
            or acknowledgement.get("reviewedRevision") != (run.result.get("articleExport") or {}).get("revision")
            or user_stop_intent(run) or user is None or not user.is_active or not may_publish(user, run.organization)
            or not article_review_identity_is_complete(run) or article_review_identity(run) != expected_identity):
        raise RunOwnershipConflict("The current article approval was not accepted.")
    try:
        _validate_authenticated_content_factory_actor(user=user, actor_id=actor_id)
    except (ArticleGenerationError, ValueError) as exc:
        raise RunOwnershipConflict("The requesting approval actor changed.") from exc
    require_event_identity(run, acknowledgement)
    require_event_execution(run, acknowledgement, current=True)


def prepare_owned_control(*, run, user, action, context):
    if not authoritative_run(run):
        from .run_ownership import admission_enabled
        from .run_state import ARTICLE_WORKFLOWS
        if admission_enabled() and run.workflow in ARTICLE_WORKFLOWS and action in {"approve", "revise"}:
            return Response({"detail": "Open the article review and create a new reviewed revision before continuing.",
                "next_action": "review_article", "runId": run.run_id}, status=409)
        return None
    from founder_tools.services import founder_actor_id_for_user
    from .run_revision_child_admission import require_revision_requester
    if action == "deny":
        from .run_user_stop import record_user_denial
        try:
            record_user_denial(run, user=user, actor_id=founder_actor_id_for_user(user))
        except ValueError:
            return Response({"detail": "Current startup permission is required to deny this run."}, status=403)
        return None
    if action == "cancel":
        return None
    try:
        require_revision_requester(run, user, setup=run.workflow == "article_system_setup",
            publish=action in {"approve", "promote-bundle", "publish-pr", "merge-publish-pr"})
    except ValueError:
        return Response({"detail": "The current article permission or execution consent changed."}, status=409)
    if action in {"revise", "regenerate-image", "regenerate-images"}:
        return Response({"detail": "Open the current article review and submit a separately reviewed update.",
            "next_action": "review_article", "runId": run.run_id}, status=409)
    if action == "approve" and run.workflow in {"article_generation", "direct_generate", "confirmed_topic", "article_revision"}:
        from .run_owned_observations import refresh_owned_run
        from .article_publish_approval import article_publish_approval_receipt, article_publish_approval_receipt_matches
        from . import vibe_marketing_views as views
        refresh_owned_run(run)
        if (article_publish_approval_receipt_matches(run)
                and article_publish_approval_receipt(run).get("actor_id") == founder_actor_id_for_user(user)):
            return Response(views._serialize_run(run, context=context), status=202)
    return None


def owned_control_response(*, run, remote_run, user, context, action, payload, remote_data, expected_identity,
                           approval_required, admitted_publish_child):
    if not authoritative_run(run):
        return None
    from . import vibe_marketing_views as views
    from .run_owned_observations import refresh_owned_run
    from founder_tools.services import founder_actor_id_for_user
    code = int(remote_data.get("content_factory_status_code") or 0)
    if code >= 400 or remote_data.get("allowed") is False or remote_data.get("error") or remote_data.get("status") == "noop":
        pending = action in {"promote-bundle", "publish-pr"} and views._content_factory_action_transport_pending(remote_data)
        if not pending:
            status = code if 400 <= code < 600 else 409 if remote_data.get("allowed") is False or remote_data.get("status") == "noop" else 502
            return Response({"detail": remote_data.get("detail") or remote_data.get("error")
                or "Content Factory did not accept this command.", "runId": run.run_id,
                "retryable": bool(remote_data.get("retryable"))}, status=status)
    # All network reads happen before any response projection/SQL guard.
    refresh_owned_run(run)
    if admitted_publish_child is not None and action in {"promote-bundle", "publish-pr"}:
        from .run_revision_child_admission import record_publish_handoff
        admitted_publish_child.refresh_from_db()
        pending = views._content_factory_action_transport_pending(remote_data)
        for source in (run,) if run.pk == remote_run.pk else (run, remote_run):
            record_publish_handoff(run=source, child=admitted_publish_child, pending=pending)
        return Response(views._serialize_run(admitted_publish_child, context=context), status=202)
    if action in {"promote-bundle", "publish-pr"}:
        return Response({"detail": "A separately admitted publication child is required.", "runId": run.run_id}, status=409)
    if action == "approve" and approval_required:
        if not views._record_article_publish_approval_receipt(run, actor_id=founder_actor_id_for_user(user),
                expected_identity=expected_identity, accepted_control=remote_data, user=user):
            return views._article_publish_approval_receipt_failure(run)
        from .article_review_views import record_review_approval
        record_review_approval(run, context, payload)
    elif action == "approve":
        try:
            if (remote_data.get("action") != "approve" or remote_data.get("accepted") is not True
                    or remote_data.get("run_id") != run.run_id):
                raise RunOwnershipConflict("The current approval acknowledgement is unavailable.")
            require_event_identity(run, remote_data)
            require_event_execution(run, remote_data, current=True)
        except ValueError:
            return Response({"detail": "The current approval acknowledgement is unavailable.", "runId": run.run_id}, status=409)
    return Response(views._serialize_run(run, context=context), status=202)
