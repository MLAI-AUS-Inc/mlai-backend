"""Current private CMS article and original billing, before any provider effect."""

from dataclasses import dataclass, field
from copy import deepcopy

from . import cms_artifact_review as reviews
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The original CMS article, payment or review changed.")


@dataclass(frozen=True)
class CurrentCmsArticle:
    """A local observation only; callers recheck around each external effect."""

    run: object = field(repr=False)
    admitted: object = field(repr=False)
    review_operation_id: str
    handoff: dict = field(repr=False)
    billing: dict = field(repr=False)


def original_billing(run):
    """Resolve this company's actual original spend, or its saved free decision.

    Today's price, the requesting actor and a worker authorization flag cannot
    replace the immutable original outbox payment identity. This does not bill.
    """
    from roo.models import Ledger
    from .billing_lineage import ARTICLE_CHARGE_PREFIX, original_article_spend_for_run
    from .billing import normalize_content_factory_domain
    from .channel_artifacts import authorize_payload
    admitted = authorize_payload(run.run_request)
    request = run.run_request
    result = run.result if isinstance(run.result, dict) else {}
    terminal = result.get("article_terminal_refund") or {}
    if (str(run.organization_id) != admitted.envelope["organization_id"] or
            normalize_content_factory_domain(run.domain) != normalize_content_factory_domain(request.get("domain")) or
            request.get("roo_points_authorized") is not True or request.get("roo_points_action") != "article_generation" or
            type(request.get("roo_points_cost")) is not int or not 0 <= request["roo_points_cost"] < 10**9 or
            request.get("pending_billing_refund") or request.get("roo_points_refund_ledger_id") or
            result.get("reconciliation_refund_pending") or
            not isinstance(terminal, dict) or terminal.get("status") not in (None, "not_required") or
            request.get("roo_points_billing_status") not in {"free", "charged", "reused"}):
        raise _deny()
    payer = request.get("roo_points_billing_user_id")
    if isinstance(payer, bool) or not str(payer or "").isdecimal() or not 0 < int(payer) < 2**63:
        raise _deny()
    if request["roo_points_billing_status"] == "free":
        if request["roo_points_cost"] != 0 or request.get("roo_points_ledger_id") not in (None, ""):
            raise _deny()
        key, ledger_id = request["client_request_id"], None
    else:
        ledger = original_article_spend_for_run(run)
        if (ledger is None or request["roo_points_cost"] <= 0 or
                str(ledger.user_id) != str(payer) or ledger.delta_microroo != -request["roo_points_cost"] * 1_000_000 or
                str(ledger.pk) != request.get("roo_points_ledger_id") or
                not str(ledger.idempotency_key).startswith(ARTICLE_CHARGE_PREFIX)):
            raise _deny()
        key = ledger.idempotency_key.removeprefix(ARTICLE_CHARGE_PREFIX)
        ledger_id = str(ledger.pk)
    # The credit may have committed before the run/outbox refund stamp. Check
    # that original idempotency key even when the current quote became free.
    if Ledger.objects.filter(idempotency_key="content_factory:refund:" + key).exists():
        raise _deny()
    receipt = {"schema_version": 1, "kind": "cms_original_article_billing", "basis": request["roo_points_billing_status"],
        "company_id": admitted.envelope["company_id"], "organization_id": admitted.envelope["organization_id"],
        "billing_request_id": key, "spend_ledger_id": ledger_id, "payer_user_id": str(payer),
        "cost_points": request["roo_points_cost"], "artifact_envelope_digest": admitted.envelope["digest"]}
    return {**receipt, "digest": digest(receipt)}


def current_article(operation):
    """Recheck a private collected artifact and original billing, without HTTP."""
    from workflow_runs.models import ContentFactoryRun
    try:
        payload = operation.payload
        if (operation.action != reviews.ACTION or operation.state != "completed" or not isinstance(payload, dict) or
                set(payload) != reviews.IDENTITY | {"failures", "handoff"} or
                type(payload["failures"]) is not int or not 0 <= payload["failures"] <= 5):
            raise _deny()
        run = ContentFactoryRun.objects.filter(run_id=payload["source_run_id"]).first()
        admitted = reviews._source(run)
        handoff = reviews.validate_review_surface(payload["handoff"])
        if ({key: payload[key] for key in reviews.IDENTITY} != reviews._identity(run, admitted, payload["expected_revision"]) or
                operation.generation != admitted.scope.binding["generation"] or
                str(operation.connection_id) != admitted.scope.binding["connection_id"] or
                handoff["run_id"] != run.run_id or handoff["run_generation"] != run.result["generation"] or
                handoff["envelope"] != admitted.envelope or
                payload["expected_revision"] is not None and payload["expected_revision"] != handoff["review_revision"] or
                operation.receipt != {"status": "artifact_received", "channel": "cms", "sourceRunId": run.run_id,
                    "artifactDigest": handoff["digest"], "reviewRevision": handoff["review_revision"],
                    "provider_modified": False, "publishing_verified": False, "approval_recorded": False}):
            raise _deny()
        return CurrentCmsArticle(run, admitted, str(operation.pk), deepcopy(handoff), original_billing(run))
    except WebsiteAuthorityError:
        raise
    except (reviews.CmsArtifactError, ValueError, TypeError, KeyError, AttributeError):
        raise _deny() from None
