"""Founder-facing Pulse run messages: plain sentences, never internal details.

Workers and providers report errors with URLs, response bodies and provider
codes. Those stay in the logs and the stored run. What a founder sees is one
of the sentences below.
"""

import logging

from startup_updates.preflight import run_skipped_sources, source_label

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "MLAI could not finish this draft. Your progress is saved, so please try again."
SERVICE_BUSY = "MLAI's drafting service is busy right now. Your progress is saved, so please try again later."
SOURCES_NEED_ATTENTION = "Some of your tools need attention before MLAI can draft this update. Check them below."
NOTHING_TO_READ = (
    "MLAI could not read any of your tools for this month. "
    "Reconnect a tool or add notes or a document, then try again."
)
NO_FACTS = (
    "MLAI read your tools but found nothing to report for this month. "
    "Add notes or a document, then try again."
)

# Sentences our own code writes for founders. They are shown unchanged.
_SAFE_PREFIXES = (
    "pulse_context_limit:",  # The chat app rewrites this one itself.
    "MLAI ",
    "Your draft is saved",
    "Your update is saved",
    "This run used an older source range",
    "This draft was started with an older source period",
    "Draft generation failed. Please try again.",
    "Draft generation completed without producing a draft.",
    "Automatic recovery reached its limit",
    "The draft changed",
    "The saved draft changed",
    "The update changed after generation started.",
)


def founder_failure_message(text):
    """Map a stored run or step error to a sentence a founder can act on."""
    raw = str(text or "").strip()
    if not raw:
        return GENERIC_FAILURE
    lowered = raw.lower()
    if lowered.startswith(("selected sources need attention", "check the selected sources")):
        return SOURCES_NEED_ATTENTION
    if "no usable facts" in lowered:
        return NO_FACTS
    if any(code in lowered for code in ("insufficient_quota", "credit_balance_exhausted", "rate limit", "rate_limit", "overloaded")):
        return SERVICE_BUSY
    if raw.startswith(_SAFE_PREFIXES) and "http" not in lowered and len(raw) <= 400:
        return raw
    logger.info("Pulse run error replaced with a founder message", extra={"error_preview": raw[:200]})
    return GENERIC_FAILURE


def _plain_warning(provider, text):
    label = source_label(provider)
    capitalised = label[:1].upper() + label[1:]
    if "reconnect" in str(text).lower():
        return {"provider": provider, "code": "needs_reconnect", "action": "reconnect",
            "message": f"{capitalised} needs reconnecting for MLAI to read all of it."}
    return {"provider": provider, "code": "partially_read", "action": "retry",
        "message": f"MLAI could not read all of {label} for this draft."}


def run_source_warnings(run, legacy_warnings=None):
    """List what this run left out or only partly read, one entry per tool.

    Each entry is ``{"provider", "code", "message", "action"}`` where action is
    "reconnect", "retry" or None.
    """
    if run is None:
        return []
    request, result = run.run_request or {}, run.result or {}
    warnings = []
    seen = set()
    for provider, entry in run_skipped_sources(request, result).items():
        warnings.append({"provider": provider, "code": entry.get("code") or "skipped",
            "message": entry.get("message") or f"MLAI left {source_label(provider)} out of this draft.",
            "action": entry.get("action")})
        seen.add(provider)
    skipped_rows = int((((result.get("event_source_progress") or {}).get("bank_feed")) or {}).get("skipped_rows") or 0)
    if skipped_rows and "bank_feed" not in seen:
        noun = "transaction" if skipped_rows == 1 else "transactions"
        warnings.append({"provider": "bank_feed", "code": "rows_skipped", "action": None,
            "message": f"MLAI left out {skipped_rows} bank {noun} it could not read."})
        seen.add("bank_feed")
    for provider, texts in (legacy_warnings or {}).items():
        if provider in seen or not texts:
            continue
        warnings.append(_plain_warning(provider, " ".join(map(str, texts))))
        seen.add(provider)
    return warnings
