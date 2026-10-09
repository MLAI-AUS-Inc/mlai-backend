"""Small continuity context for curation, separate from editable evidence receipts."""


def prior_update_context(draft, *, published=False):
    """Return narrative and revision identity without loading an evidence snapshot."""
    revision = draft.published_revision if published else draft.current_revision
    memo = (revision.structured_memo if revision else draft.structured_memo) or {}
    memo = memo if isinstance(memo, dict) else {}
    title = memo.get("title")
    update_date = memo.get("update_date")
    period = memo.get("narrative_period")
    if isinstance(period, dict):
        period = {
            key: value
            for key, value in period.items()
            if key in {"start", "end", "timezone", "end_exclusive"}
            and isinstance(value, (str, bool))
        }
    else:
        period = None
    return {
        "id": draft.pk,
        "month": draft.month.isoformat(),
        "title": title if isinstance(title, str) and title else draft.title,
        "updateDate": update_date if isinstance(update_date, str) else None,
        "narrativePeriod": period,
        "published_at": draft.published_at.isoformat() if draft.published_at else None,
        "revisionId": revision.pk if revision else None,
        "revisionHash": revision.content_hash if revision else None,
        "rendered_markdown": revision.rendered_markdown
        if revision
        else draft.rendered_markdown,
    }
