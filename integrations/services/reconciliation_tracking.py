"""One-dimensional Xero tracking policy for statement reconciliation."""

from __future__ import annotations

from typing import Any

from integrations.models import ReconciliationProfile, XeroStatementSuggestion


def infer_allocation_mode(suggestion: XeroStatementSuggestion) -> str:
    """Return a backwards-compatible allocation mode for stored suggestions."""

    if suggestion.allocation_mode != XeroStatementSuggestion.ALLOCATION_UNASSIGNED:
        return suggestion.allocation_mode
    if suggestion.event_tracking_option_name or suggestion.event_source_id:
        return XeroStatementSuggestion.ALLOCATION_EVENT
    if suggestion.project_tracking_option_name or suggestion.project_source_id:
        return XeroStatementSuggestion.ALLOCATION_PROJECT
    return XeroStatementSuggestion.ALLOCATION_UNASSIGNED


def effective_tracking(
    profile: ReconciliationProfile,
    suggestion: XeroStatementSuggestion,
) -> dict[str, Any] | None:
    """Resolve the single tracking assignment included in an approval preview."""

    mode = infer_allocation_mode(suggestion)
    if mode == XeroStatementSuggestion.ALLOCATION_EVENT:
        option_name = suggestion.event_tracking_option_name
        if not option_name:
            return None
        return {
            "allocation_mode": mode,
            "kind": "event",
            "category_id": profile.event_tracking_category_id,
            "category_name": profile.event_tracking_category_name,
            "option_id": "",
            "option_name": option_name,
            "default": False,
        }
    if mode in {
        XeroStatementSuggestion.ALLOCATION_PROJECT,
        XeroStatementSuggestion.ALLOCATION_MLAI_CORE,
    }:
        default = mode == XeroStatementSuggestion.ALLOCATION_MLAI_CORE
        option_name = (
            profile.default_project_tracking_option_name
            if default
            else suggestion.project_tracking_option_name
        )
        option_id = (
            profile.default_project_tracking_option_id
            if default
            else suggestion.project_tracking_option_id
        )
        if not option_name:
            return None
        return {
            "allocation_mode": mode,
            "kind": "project",
            "category_id": profile.project_tracking_category_id,
            "category_name": profile.project_tracking_category_name,
            "option_id": option_id,
            "option_name": option_name,
            "default": default,
        }
    return None


def effective_tracking_errors(
    profile: ReconciliationProfile,
    suggestion: XeroStatementSuggestion,
) -> list[str]:
    """Validate mutual exclusivity and the organisation's mandatory policy."""

    errors: list[str] = []
    has_event = bool(suggestion.event_source_id or suggestion.event_tracking_option_name)
    has_project = bool(suggestion.project_source_id or suggestion.project_tracking_option_name)
    mode = infer_allocation_mode(suggestion)
    if has_event and has_project:
        errors.append("Choose exactly one Event Name or Project Name, not both.")
    if mode == XeroStatementSuggestion.ALLOCATION_EVENT and not has_event:
        errors.append("Event allocation requires one known Event Name.")
    if mode == XeroStatementSuggestion.ALLOCATION_PROJECT and not has_project:
        errors.append("Project allocation requires one known Project Name.")
    if mode == XeroStatementSuggestion.ALLOCATION_MLAI_CORE and (has_event or has_project):
        errors.append("MLAI core allocation cannot also contain a specific Event or Project.")
    if mode == XeroStatementSuggestion.ALLOCATION_UNASSIGNED and (has_event or has_project):
        errors.append("The allocation mode does not match the selected tracking option.")

    assignment = effective_tracking(profile, suggestion)
    if profile.require_statement_tracking and assignment is None:
        errors.append("Every execution-ready suggestion must have an Event, Project, or MLAI core allocation.")
    if assignment and not assignment["category_id"]:
        errors.append(f"Configure the Xero {assignment['category_name']} tracking category ID.")
    return errors


def xero_tracking_entry(assignment: dict[str, Any]) -> dict[str, str]:
    """Translate a normalized effective assignment into a Xero Tracking item."""

    values = {
        "TrackingCategoryID": str(assignment.get("category_id") or ""),
        "Name": str(assignment.get("category_name") or ""),
        "TrackingOptionID": str(assignment.get("option_id") or ""),
        "Option": str(assignment.get("option_name") or ""),
    }
    return {key: value for key, value in values.items() if value}
