"""Content that lives in a CMS or database: an honest stop at repository selection (plan C15).

Content Factory's free inventory scan classifies where a site's articles live
(``content_source``, CF ``cms_content.py``). WordPress, Ghost and Drupal sites,
and sites whose blog is fed by a headless CMS, are the one shape no setup tier
can wire: the posts are not in the repository. Owner decision D18 keeps a
CMS-API publisher out of scope, so such a site never enters the setup ladder.

Preparation checks the classification after the scan and before it would
dispatch a setup run, so nothing is built, charged or alerted:

- the preparation closes as "can't be done yet" with reason ``cms_content``
  (the same closed-list state as a B25 staff close, so every client already
  renders it, with no Try again);
- any goal reservation is released through the usual refund (D7);
- one lead row is written on the founder's CRM contact (the A21 lead path),
  so interest is counted for D18; "Tell me when this changes" upgrades it;
- no staff alert: there is nothing for staff to fix.

The stop is pinned to the scanned source SHA. A later scan of a different
source that is not classified as CMS content is read afresh.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

REASON = "cms_content"
LEAD_REASON = "cms_content"
CMS_KINDS = frozenset({"wordpress", "drupal", "ghost", "headless_cms"})
MESSAGE = ("We can't add articles to this website yet: its posts live in a content system ({cms}), not in your "
           "website's code, and MLAI publishes by changing your code. Nothing was charged. We've noted your "
           "interest and can tell you when this changes.")
CMS_NAMES = {"wordpress": "WordPress", "drupal": "Drupal", "ghost": "Ghost", "headless_cms": "a headless CMS"}


def classification(discovery, *, source_sha=None):
    """The CMS classification of ``discovery`` (the journey's ``discovery``), or None.

    Only a complete scan counts, and only of the current source when one is known.
    """
    discovery = discovery if isinstance(discovery, dict) else {}
    source = discovery.get("contentSource")
    if not isinstance(source, dict) or source.get("kind") != "cms" or source.get("cms") not in CMS_KINDS:
        return None
    if discovery.get("complete") is not True:
        return None
    if source_sha and discovery.get("sourceSha") and discovery.get("sourceSha") != source_sha:
        return None
    return source


def setup_already_ran(op, journey, *, source_sha=""):
    """True when this website is past repository selection, so C15 must not stop it.

    The stop belongs before any paid setup. A preparation that already
    dispatched or merged a setup run, a journey still pointing at an open setup
    pull request, a verified integration, or a settled setup run on this
    connection all mean the decision was made earlier; a later scan never
    closes a working site as "can't be done yet".
    """
    receipt = op.receipt if isinstance(op.receipt, dict) else {}
    if receipt.get("setup_run_id") or receipt.get("merged_setup_run_id"):
        return True
    journey = journey if isinstance(journey, dict) else {}
    if (journey.get("nextAction") or {}).get("setupRunId"):
        return True
    integration = (journey.get("prerequisites") or {}).get("integration") or {}
    if integration.get("status") == "complete":
        return True
    from .website_connections import settled_setup_run
    return bool(settled_setup_run(op.connection, source_sha=source_sha))


def stop(op, source, *, now=None, source_sha=""):
    """Close preparation ``op`` as "can't be done yet: cms_content". The caller persists ``op``."""
    now = now or timezone.now()
    receipt = dict(op.receipt or {})
    receipt.pop("user_action", None)
    receipt.pop("hold_owner", None)
    receipt.pop("channel_options", None)
    cms = source.get("cms")
    receipt.update(
        # cms_content is a closed "can't be done yet" reason (the B25 list,
        # website_staff_fixing.CANT_BE_DONE_REASONS), carried in
        # ``cant_be_done_yet.reason`` like a staff close; it is not a failure
        # registry code, so no unregistered reason_code is emitted. Any code
        # left from an earlier attempt is cleared.
        status="cant_be_done_yet", reason_code="", failure_class="unsupported", retryable=False,
        current_step="scan", blocked_step="setup",
        message=MESSAGE.format(cms=CMS_NAMES.get(cms, "a CMS")),
        cant_be_done_yet={"version": 1, "reason": REASON, "decided_at": now.isoformat(), "stage": "repository_selection",
                          "cms": cms, "vendor": str(source.get("vendor") or "")[:40] or None,
                          "source_sha": str(source_sha or "")[:64] or None,
                          "signals": [str(item)[:200] for item in (source.get("signals") or [])[:8]]})
    op.receipt = receipt
    op.state, op.next_attempt_at = "stopped", None
    _release_and_record_lead(op, cms=cms, now=now)
    return op


def _release_and_record_lead(op, *, cms, now):
    from .website_first_article_refunds import reserve_refund
    try:
        reserve_refund(op, now=now)
    except Exception:  # noqa: BLE001 - the stop stands; a missing reservation is logged
        logger.error("cms_content_refund_reservation_failed operation=%s", op.pk, exc_info=True)
    try:
        record_lead(op.connection.organization, cms=cms, now=now)
    except Exception:  # noqa: BLE001 - a lead row never blocks the founder's answer
        logger.error("cms_content_lead_row_failed operation=%s", op.pk, exc_info=True)


def record_lead(organization, *, cms, now=None, notify=False):
    """One CMS-content lead on the startup owner's CRM contact (A21 lead path)."""
    from django.contrib.auth import get_user_model
    from founder_tools.website_leads import normalise_lead_domain, record_lead as record
    user = get_user_model().objects.filter(pk=getattr(organization, "billing_user_id", None)).first()
    if user is None:
        logger.info("cms_content_lead_without_owner org=%s", getattr(organization, "pk", None))
        return None
    return record(user, {"reason": LEAD_REASON, "domain": normalise_lead_domain(getattr(organization, "domain", "")),
                         "hostClass": cms if cms in CMS_KINDS else "unknown", "notify": bool(notify)},
                  now=now, source="repository_selection.cms_content")
