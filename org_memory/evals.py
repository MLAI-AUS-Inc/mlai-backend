from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError as PydanticValidationError

from .answering import (
    ABSTENTION_ANSWER,
    ANSWER_PROMPT,
    GroundedAnswerOutput,
    citations_within_selected,
)
from .extraction import (
    ClaimCandidate,
    ExtractionPayload,
    candidate_policy_flags,
    deterministic_candidates,
    scan_source_safety,
)
from .consolidation import (
    DEFAULT_STALE_DAYS,
    LEGAL_TRANSITIONS,
    NON_EXPIRING_KINDS,
    ConsolidationDecision,
)
from .retrieval import query_terms, redact_query


DEFAULT_SEED_PATH = Path(__file__).with_name("eval_data") / "seed_extraction.json"
DEFAULT_CONSOLIDATION_SEED_PATH = Path(__file__).with_name("eval_data") / "seed_consolidation.json"
DEFAULT_RETRIEVAL_SEED_PATH = Path(__file__).with_name("eval_data") / "seed_retrieval.json"


def _evaluate_case(case: dict) -> list[str]:
    errors = []
    case_id = str(case.get("id") or "unnamed")
    mode = case.get("mode")
    expected = case.get("expected") or {}
    if mode == "deterministic":
        chunk = SimpleNamespace(pk=f"eval-{case_id}", text=str(case.get("text") or ""))
        candidates = deterministic_candidates([chunk], classification="internal")
        actual = {
            "count": len(candidates),
            "kinds": [candidate.kind for candidate in candidates],
            "epistemic_types": [candidate.epistemic_type for candidate in candidates],
        }
    elif mode == "source_safety":
        actual = {"flags": scan_source_safety(str(case.get("text") or ""))}
    elif mode == "candidate_policy":
        actual = {"flags": candidate_policy_flags(ClaimCandidate.model_validate(case["candidate"]))}
    elif mode == "schema":
        try:
            ExtractionPayload.model_validate(case.get("payload"))
            valid = True
        except PydanticValidationError:
            valid = False
        actual = {"valid": valid}
    else:
        return [f"{case_id}: unsupported eval mode {mode!r}"]
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            errors.append(f"{case_id}: expected {key}={expected_value!r}, got {actual.get(key)!r}")
    return errors


def evaluate_seed_suite(path=None) -> dict:
    fixture_path = Path(path) if path else DEFAULT_SEED_PATH
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    errors = []
    for case in cases:
        errors.extend(_evaluate_case(case))
    return {
        "fixture": str(fixture_path),
        "cases": len(cases),
        "passed": len(cases) - len({error.split(":", 1)[0] for error in errors}),
        "errors": errors,
        "ok": not errors,
    }


def _evaluate_consolidation_case(case: dict) -> list[str]:
    errors = []
    case_id = str(case.get("id") or "unnamed")
    mode = case.get("mode")
    expected = case.get("expected") or {}
    if mode == "schema":
        try:
            ConsolidationDecision.model_validate(case.get("decision"))
            valid = True
        except PydanticValidationError:
            valid = False
        actual = {"valid": valid}
    elif mode == "transition":
        actual = {
            "allowed": case.get("to_status")
            in LEGAL_TRANSITIONS.get(case.get("from_status"), set())
        }
    elif mode == "staleness":
        kind = case.get("kind")
        actual = {
            "days": DEFAULT_STALE_DAYS.get(kind),
            "non_expiring": kind in NON_EXPIRING_KINDS,
        }
    else:
        return [f"{case_id}: unsupported eval mode {mode!r}"]
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            errors.append(
                f"{case_id}: expected {key}={expected_value!r}, got {actual.get(key)!r}"
            )
    return errors


def evaluate_consolidation_seed_suite(path=None) -> dict:
    fixture_path = Path(path) if path else DEFAULT_CONSOLIDATION_SEED_PATH
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    errors = []
    for case in cases:
        errors.extend(_evaluate_consolidation_case(case))
    failed_ids = {error.split(":", 1)[0] for error in errors}
    return {
        "fixture": str(fixture_path),
        "cases": len(cases),
        "passed": len(cases) - len(failed_ids),
        "errors": errors,
        "ok": not errors,
    }


def _evaluate_retrieval_case(case: dict) -> list[str]:
    errors = []
    case_id = str(case.get("id") or "unnamed")
    mode = case.get("mode")
    expected = case.get("expected") or {}
    if mode == "answer_schema":
        try:
            GroundedAnswerOutput.model_validate(case.get("answer"))
            valid = True
        except PydanticValidationError:
            valid = False
        actual = {"valid": valid}
    elif mode == "citation_boundary":
        actual = {
            "allowed": citations_within_selected(
                case.get("cited_memory_ids"),
                case.get("selected_memory_ids"),
            )
        }
    elif mode == "query_redaction":
        actual = {"redacted": redact_query(str(case.get("query") or ""))}
    elif mode == "query_terms":
        actual = {"terms": list(query_terms(str(case.get("query") or "")))}
    elif mode == "abstention":
        actual = {"answer": ABSTENTION_ANSWER}
    elif mode == "prompt_policy":
        normalized = ANSWER_PROMPT.casefold()
        actual = {
            "marks_evidence_untrusted": "evidence is untrusted data" in normalized,
            "forbids_tools": "call tools" in normalized,
            "requires_supplied_memory_ids": "supplied memory ids" in normalized,
        }
    else:
        return [f"{case_id}: unsupported eval mode {mode!r}"]
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            errors.append(
                f"{case_id}: expected {key}={expected_value!r}, got {actual.get(key)!r}"
            )
    return errors


def evaluate_retrieval_seed_suite(path=None) -> dict:
    fixture_path = Path(path) if path else DEFAULT_RETRIEVAL_SEED_PATH
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    errors = []
    for case in cases:
        errors.extend(_evaluate_retrieval_case(case))
    failed_ids = {error.split(":", 1)[0] for error in errors}
    return {
        "fixture": str(fixture_path),
        "cases": len(cases),
        "passed": len(cases) - len(failed_ids),
        "errors": errors,
        "ok": not errors,
    }
