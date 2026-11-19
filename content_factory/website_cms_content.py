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

CODE = "cms_content"
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


def stop(op, source, *, now=None, source_sha=""):
    """Close preparation ``op`` as "can't be done yet: cms_content". The caller persists ``op``."""
    now = now or timezone.now()
    receipt = dict(op.receipt or {})
    receipt.pop("user_action", None)
    receipt.pop("hold_owner", None)
    receipt.pop("channel_options", None)
    cms = source.get("cms")
    receipt.update(
        status="cant_be_done_yet", reason_code=CODE, failure_class="unsupported", retryable=False,
        current_step="scan", blocked_step="setup",
        message=MESSAGE.format(cms=CMS_NAMES.get(cms, "a CMS")),
        cant_be_done_yet={"version": 1, "reason": CODE, "decided_at": now.isoformat(), "stage": "repository_selection",
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
