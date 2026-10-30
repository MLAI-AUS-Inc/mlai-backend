"""Allowlisted export of an exact current Content Factory article snapshot."""

# Bounds for the copy formats a new-journey founder pastes (IF-P1-12).
MAX_EXPORT_HTML_BYTES = 4_000_000
MAX_META_TITLE_CHARS = 10_000

from urllib.parse import urlsplit


def public_export_url(value):
    """Accept bounded HTTPS asset links without embedded credentials."""
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def article_export(review, *, run_id, latest_run_id=None, allow_preview_pending=False, copy_formats=False):
    """Fail closed on missing, pending, superseded or foreign saved revisions.

    ``allow_preview_pending`` (a content_only run of a new-journey org with
    ``ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED``, decided by the caller) accepts a
    ready saved export while the preview is still rebuilding, and then reports
    ``previewPending: true``. ``copy_formats`` (new-journey orgs only) passes
    the saved export's ``html`` and ``metaTitle`` through. The superseded,
    revision and canonical checks hold either way, and with both left False the
    export is exactly as before.
    """
    review = review if isinstance(review, dict) else {}
    revision = review.get("revision")
    unavailable = {"version": 1, "status": "unavailable", "runId": run_id,
        "revision": revision if isinstance(revision, str) else None,
        "format": "markdown", "markdown": "", "metadata": {}, "media": []}
    if latest_run_id and latest_run_id != run_id or review.get("latestRunId") and review["latestRunId"] != run_id:
        return {**unavailable, "reasonCode": "revision_superseded"}
    pending_allowed = allow_preview_pending is True and review.get("previewPending") is True
    if review.get("previewPending") is not False and not pending_allowed:
        return {**unavailable, "reasonCode": "preview_pending"}
    saved = review.get("articleExport")
    if not isinstance(saved, dict) or saved.get("version") != 1 or isinstance(saved.get("version"), bool):
        return {**unavailable, "reasonCode": "canonical_snapshot_unavailable"}
    if (not isinstance(revision, str) or not revision or saved.get("revision") != revision
            or saved.get("runId") != run_id or saved.get("format") != "markdown"):
        return {**unavailable, "reasonCode": "revision_mismatch"}
    if saved.get("status") != "ready":
        reason = saved.get("reasonCode")
        return {**unavailable, "reasonCode": reason if reason in {"revision_superseded", "preview_pending", "revision_mismatch", "canonical_snapshot_unavailable"} else "canonical_snapshot_unavailable"}
    markdown = saved.get("markdown")
    if not isinstance(markdown, str) or not markdown or len(markdown.encode("utf-8")) > 2_000_000:
        return {**unavailable, "reasonCode": "canonical_snapshot_unavailable"}
    metadata = saved.get("metadata") if isinstance(saved.get("metadata"), dict) else {}
    allowed_metadata = {key: metadata[key] for key in ("title", "slug", "description")
        if isinstance(metadata.get(key), str) and len(metadata[key]) <= 10_000}
    canonical = public_export_url(metadata.get("canonical_url"))
    if canonical:
        allowed_metadata["canonical_url"] = canonical
    media = []
    for item in saved.get("media", [])[:100] if isinstance(saved.get("media"), list) else []:
        if not isinstance(item, dict) or not public_export_url(item.get("url")):
            continue
        media.append({"url": item["url"], **{key: item[key] if isinstance(item.get(key), str) and len(item[key]) <= 10_000 else "" for key in ("alt", "caption")}})
    ready = {**unavailable, "status": "ready", "markdown": markdown, "metadata": allowed_metadata, "media": media}
    if pending_allowed:
        ready["previewPending"] = True
    if copy_formats is True:
        ready.update(_copy_formats(saved))
    return ready


def _copy_formats(saved):
    """The saved export's ``html`` and ``metaTitle``, when present and bounded."""
    formats = {}
    html = saved.get("html")
    if isinstance(html, str) and html and len(html.encode("utf-8")) <= MAX_EXPORT_HTML_BYTES:
        formats["html"] = html
    title = saved.get("metaTitle")
    if isinstance(title, str) and title.strip() and len(title) <= MAX_META_TITLE_CHARS:
        formats["metaTitle"] = title
    return formats
