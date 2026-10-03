"""Bounded answer evidence for baseline summary responses.

These projections do not change stored measurements or their scoring. Older
snapshots can supply excerpts without pretending to contain a full answer.
"""

from datetime import datetime
import re
from urllib.parse import urlsplit


AI_PROMPT_LIMIT = 40
AI_ANSWER_TEXT_LIMIT = 16000
AI_PROVIDERS = frozenset({"chatgpt", "claude", "gemini", "perplexity"})
_ANSWER_STATUSES = frozenset({"measured", "error", "unavailable", "needs_connection"})
_REFERENCE_KINDS = frozenset({"mentioned", "recommended", "cited_source", "compared", "listed"})
_METADATA_TEXT_FIELDS = {
    "modelName": 200,
    "surface": 100,
    "measurementKind": 100,
    "measurementType": 100,
    "evidenceVersion": 100,
    "locationTargeting": 200,
    "locationName": 200,
    "languageCode": 16,
}


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _urls(value, limit):
    if not isinstance(value, list):
        return []
    result = []
    for raw in value[:100]:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url or len(url) > 2048 or re.search(r"\s|[\x00-\x1f\x7f]", url):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                continue
            if parsed.username is not None or parsed.password is not None:
                continue
            _port = parsed.port
        except ValueError:
            continue
        if url not in result:
            result.append(url)
        if len(result) == limit:
            break
    return result


def ai_answer_metadata(data):
    """Retain typed provenance without inferring geography for older snapshots."""
    result = {}
    for field, limit in _METADATA_TEXT_FIELDS.items():
        value = _text(data.get(field), limit)
        if value:
            result[field] = value
    for field in ("countryCode", "requestedCountryCode", "actualCountryCode"):
        value = data.get(field)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2}", value):
            result[field] = value.upper()
        elif field in data and value is None:
            result[field] = None
    for field in ("webSearchUsed", "countryApplied"):
        if isinstance(data.get(field), bool):
            result[field] = data[field]
        elif field in data and data[field] is None:
            result[field] = None
    captured_at = _text(data.get("capturedAt"), 64)
    if captured_at:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                result["capturedAt"] = captured_at
        except ValueError:
            pass
    return result


def compact_ai_prompt_evidence(value):
    """Expose successful, failed, and unavailable answers without coercing flags.

    Malformed successful rows stay unavailable rather than becoming a negative
    brand observation. Transcript bodies and links have independent limits.
    """
    if not isinstance(value, list):
        return []
    result = []
    for data in value[: AI_PROMPT_LIMIT * 2]:
        if not isinstance(data, dict):
            continue
        query = _text(data.get("query"), 500)
        prompt = _text(data.get("prompt"), 2000)
        if not query and not prompt:
            continue
        row = {"query": query or prompt, **ai_answer_metadata(data)}
        if prompt:
            row["prompt"] = prompt
        status = data.get("status")
        row["status"] = status if isinstance(status, str) and status in _ANSWER_STATUSES else "unavailable"
        error = _text(data.get("error"), 500)
        if error and row["status"] != "measured":
            row["error"] = error
        if row["status"] == "measured" and not all(
            isinstance(data.get(field), bool) for field in ("mentioned", "cited")
        ):
            row["status"] = "unavailable"
            row["reasonCode"] = "invalid_answer_evidence"
        if row["status"] == "measured":
            row.update({"mentioned": data["mentioned"], "cited": data["cited"]})
            for field, limit in (("citedUrls", 10), ("sourceUrls", 20)):
                if isinstance(data.get(field), list):
                    row[field] = _urls(data[field], limit)
            for field, limit in (("responseExcerpt", 320), ("answerText", AI_ANSWER_TEXT_LIMIT)):
                text = _text(data.get(field), limit)
                if text:
                    row[field] = text
            if "answerText" in row:
                row["answerTextTruncated"] = (
                    data.get("answerTextTruncated") is True
                    or len(data["answerText"].strip()) > AI_ANSWER_TEXT_LIMIT
                )
            contexts = []
            if isinstance(data.get("mentionContexts"), list):
                for context in data["mentionContexts"][:10]:
                    if not isinstance(context, dict):
                        continue
                    text = _text(context.get("text"), 280)
                    if text:
                        kind = context.get("kind")
                        contexts.append({"text": text, "kind": kind if isinstance(kind, str) and kind in _REFERENCE_KINDS else "mentioned"})
                    if len(contexts) == 2:
                        break
                row["mentionContexts"] = contexts
        result.append(row)
        if len(result) == AI_PROMPT_LIMIT:
            break
    return result


def compact_ai_providers(value):
    """Project four supported providers and their bounded answer observations."""
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for data in value[:40]:
        if not isinstance(data, dict):
            continue
        key = data.get("key")
        if not isinstance(key, str) or key not in AI_PROVIDERS or key in seen:
            continue
        seen.add(key)
        row = {"key": key, **ai_answer_metadata(data)}
        for field in ("label", "source", "methodVersion", "message", "reasonCode"):
            text = _text(data.get(field), 500 if field == "message" else 200)
            if text:
                row[field] = text
        status = data.get("status")
        row["status"] = status if isinstance(status, str) and status in _ANSWER_STATUSES else "unavailable"
        for field in ("responseCount", "mentionCount", "citationCount", "requestedCount"):
            number = data.get(field)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                row[field] = number
        score = data.get("score")
        if score is None or isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 100:
            row["score"] = score
        prompts = compact_ai_prompt_evidence(data.get("prompts"))
        if prompts:
            row["prompts"] = prompts
        result.append(row)
    return result
