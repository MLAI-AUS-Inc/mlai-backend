"""Bounded, preview-bound issue markers for hosted article quality review.

The visible reviewer numbers claims in the rendered article, separately from
the package grounding ledger. Only the captured image index has a safe
component mapping in the existing report. Other findings remain visible
without inventing an article location.
"""

from __future__ import annotations

from datetime import datetime
import re

from .section_issues import _safe_text


_CLAIM_ERROR = re.compile(r"^(claim-[0-9]{1,6}):\s*(.+)$")
_IMAGE_SOURCE = re.compile(r"^captured-image:([0-9]{1,3})$")
_COMPONENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,119}$")
_SECTION_ID = re.compile(r"^section:[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _generation(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value):
        return int(value)
    return None


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def public_hosted_quality_issues(quality, preview, manifest, *, resume_generation=None):
    """Project claim errors only for the exact hosted preview attempt on screen."""
    quality, preview, manifest = _mapping(quality), _mapping(preview), _mapping(manifest)
    if quality.get("status") != "blocking_findings" or preview.get("available") is not True:
        return []
    if (preview.get("exactRender") if "exactRender" in preview else preview.get("exact_render")) is not True:
        return []
    quality_url = quality.get("preview_url") or quality.get("previewUrl")
    preview_url = preview.get("previewUrl") or preview.get("preview_url")
    if not isinstance(quality_url, str) or not quality_url or quality_url != preview_url:
        return []
    quality_generation = _generation(quality.get("resume_generation", quality.get("resumeGeneration")))
    preview_generation = _generation(preview.get("resumeGeneration", preview.get("resume_generation", resume_generation)))
    if quality_generation is None or preview_generation is None or quality_generation != preview_generation:
        return []
    reviewed_at = _timestamp(quality.get("reviewed_at") or quality.get("reviewedAt"))
    preview_started_at = _timestamp(preview.get("startedAt") or preview.get("started_at"))
    if reviewed_at is None or preview_started_at is None or reviewed_at < preview_started_at:
        return []

    visible = _mapping(quality.get("visible_content_acceptance"))
    if visible.get("required") is not True or visible.get("passed") is not False:
        return []
    review = _mapping(visible.get("review"))
    attempts = review.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        return []
    claims = _mapping(attempts[0]).get("review")
    claims = _mapping(claims).get("claims")
    if not isinstance(claims, list) or len(claims) > 500:
        return []
    if any(not isinstance(item, dict) or not isinstance(item.get("claim_id"), str)
           or not isinstance(item.get("source_id"), str) for item in claims):
        return []
    claim_ids = [item["claim_id"] for item in claims]
    if len(claim_ids) != len(set(claim_ids)):
        return []
    claim_sources = {
        item["claim_id"]: item.get("source_id")
        for item in claims if isinstance(item, dict)
        and isinstance(item.get("claim_id"), str)
        and isinstance(item.get("source_id"), str)
    }

    components = manifest.get("components")
    if not isinstance(components, list) or len(components) > 2000:
        components = []
    valid_ids = [item.get("id") for item in components if isinstance(item, dict)
                 and isinstance(item.get("id"), str) and _COMPONENT_ID.fullmatch(item["id"])]
    if len(valid_ids) != len(set(valid_ids)):
        return []
    component_by_id = {
        item["id"]: item for item in components
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and _COMPONENT_ID.fullmatch(item["id"])
    }
    anatomy = _mapping(_mapping(quality.get("browser")).get("anatomy"))
    component_ids = anatomy.get("component_ids")
    if not isinstance(component_ids, list) or len(component_ids) > 2000:
        component_ids = []
    image_ids = [item for item in component_ids if item in component_by_id
                 and component_by_id[item].get("type") == "image" and component_by_id[item].get("editable") is True]
    image_count = anatomy.get("article_image_count")
    image_mapping_safe = (
        isinstance(image_count, int) and not isinstance(image_count, bool)
        and image_count == len(image_ids) and len(set(image_ids)) == len(image_ids)
        and image_count > 0
    )
    errors = visible.get("errors")
    if not isinstance(errors, list):
        return []
    issues = []
    seen = set()
    for error in errors[:100]:
        match = _CLAIM_ERROR.fullmatch(error.strip()) if isinstance(error, str) else None
        if not match or match[1] in seen or match[1] not in claim_sources:
            continue
        seen.add(match[1])
        reason = _safe_text(match[2], 320)
        if not reason:
            continue
        source = claim_sources[match[1]]
        component_id = None
        image = _IMAGE_SOURCE.fullmatch(source)
        if image and image_mapping_safe:
            index = int(image[1])
            if index < len(image_ids):
                component_id = image_ids[index]
        component = component_by_id.get(component_id) if component_id else None
        source_section = component.get("sourceSectionId") if component else None
        section_id = f"section:{source_section}" if isinstance(source_section, str) and source_section else None
        if section_id not in component_by_id or not _SECTION_ID.fullmatch(section_id or ""):
            section_id = None
        issues.append({
            "id": f"hosted:{match[1]}",
            "claimId": match[1],
            "componentId": component_id,
            "sectionId": section_id,
            "reason": reason,
            "sourceHint": "Hosted article quality review",
            "canRemoveSection": False,
        })
        if len(issues) >= 30:
            break
    return issues
