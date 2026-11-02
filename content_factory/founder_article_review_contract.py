"""Scoped additive review DTOs and current command access; never effect authority."""
from copy import deepcopy

from rest_framework.response import Response

MAX_CANDIDATE_FAILURES = 100


def poll_after_ms(review):
    """Schedule only actual pending review work, without another provider read."""
    candidates = review.get('candidates')
    pending = review.get('previewPending') is True or (isinstance(candidates, dict) and any(
        isinstance(value, dict) and value.get('status') in {'queued', 'running'}
        for value in candidates.values()))
    return 2000 if pending else 60000


def candidate_failures(review):
    """Use actual failed candidate map IDs and the closed server copy catalogue."""
    from .founder_article_errors import article_error_envelope
    candidates = review.get('candidates')
    if not isinstance(candidates, dict):
        return []
    failures = []
    for candidate_id in sorted(key for key in candidates if isinstance(key, str)):
        candidate = candidates[candidate_id]
        if (not 0 < len(candidate_id) <= 255 or not isinstance(candidate, dict)
                or candidate.get('status') != 'failed'):
            continue
        safe = article_error_envelope({'code': candidate.get('reasonCode')})['failure']
        failures.append({'candidateId': candidate_id, 'failure': safe})
        if len(failures) == MAX_CANDIDATE_FAILURES:
            break
    return failures


def _current_access(run, *, user, organization, review, latest_run=None):
    """Match current reviewer gates, without a provider read or a command."""
    access = {'canEdit': False, 'canApprove': False}
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .run_revision_child_admission import _principal, _require_original_request, _require_rights
    from .run_execution_control import require_unsettled_execution
    from . import vibe_marketing_views as views
    try:
        _principal(user)
        with startup_team._locked(organization) as (_, roster):
            current = ContentFactoryRun.objects.select_for_update(of=('self',)).filter(
                pk=run.pk, organization_id=organization.pk).first()
            if current is None:
                return access
            _require_original_request(current)
            require_unsettled_execution(current)
            setup = current.workflow == 'article_system_setup'
            try:
                _require_rights(user, organization, roster, setup=setup)
                access['canEdit'] = not (latest_run is not None
                    or (not setup and ((current.result or {}).get('article_review_approval')
                        or views._run_has_external_publish_evidence(current)))
                    or review.get('approved') is True or review.get('readOnly') is True
                    or review.get('readonly') is True)
            except ValueError:
                pass
            try:
                # The generic parent-consent command currently uses this exact
                # requester gate; it is independent of publication effects.
                _require_rights(user, organization, roster, setup=setup, publish=not setup)
                if setup:
                    access['canApprove'] = current.status in {'completed', 'awaiting_approval', 'approval_required'}
                else:
                    from .website_run_authority import event_owned
                    from .article_publish_approval import article_review_identity_is_complete
                    from .run_review_quality import verified_review_quality
                    access['canApprove'] = (verified_review_quality(current) if event_owned(current)
                        else article_review_identity_is_complete(current))
            except ValueError:
                pass
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'canEdit': False, 'canApprove': False}
    return {key: bool(value) for key, value in access.items()}


def scoped_review_response(response, *, context, run, user, latest_run=None):
    """Preserve the old success body while binding its v2 copy to resolved IDs."""
    if (not isinstance(response, Response) or not 200 <= response.status_code < 300
            or not isinstance(response.data, dict)):
        return response
    organization = getattr(context, 'organization', None)
    company = getattr(context, 'company', None)
    if organization is None or company is None:
        return response
    from .p2_foundation import p2_applies
    if not p2_applies(organization):
        return response
    original = deepcopy(response.data)
    access = _current_access(run, user=user, organization=organization, review=original, latest_run=latest_run)
    response.data = {**response.data, 'articleReview': {'contractVersion': 2,
        'companyId': str(company.pk), 'runId': run.run_id, 'review': original,
        'access': access, 'candidateFailures': candidate_failures(original),
        'pollAfterMs': poll_after_ms(original)}}
    return response
