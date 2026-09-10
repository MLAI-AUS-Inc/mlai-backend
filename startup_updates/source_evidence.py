"""Reusable reporting extraction receipts stored in the existing durable run JSON.

A receipt pins the source bytes, period, profile and extraction contract. It is
private evidence, not publication content. Old unversioned extraction is never a
cache hit. No mutable provider rows are re-read by a saved revision.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.dateparse import parse_datetime
from startup_updates.evidence_contract import content_hash

EXTRACTION_VERSION = "reporting-2026-09-10-astra-v1"


def json_value(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def classification_version(run):
    request = run.run_request or {}
    return content_hash({"version": EXTRACTION_VERSION, "context": json_value(request.get("startup_context", {})),
        "period": {key: request.get(key) for key in ("backfill_window_start", "backfill_window_end", "draft_months", "reporting_timezone")}})


def period_gmail_bundle(thread, *, start, end, attachments):
    messages = []
    excluded = 0
    for item in thread.message_payloads or []:
        timestamp = parse_datetime(str(item.get("internal_date") or ""))
        if timestamp is None or (start is not None and timestamp < start) or (end is not None and timestamp > end):
            excluded += 1
            continue
        messages.append(copy.deepcopy(item))
    message_ids = [str(item.get("message_id")) for item in messages if item.get("message_id")]
    return {
        "gmail_thread_id": thread.gmail_thread_id,
        "source_message_ids": message_ids, "source_message_count": len(messages),
        "cleaned_text": "\n\n".join(str(item.get("cleaned_text") or "") for item in messages),
        "message_payloads": messages, "attachments": attachments,
        "participant_summary": {"subjects": list(dict.fromkeys(item.get("subject", "") for item in messages))},
        "omitted_message_count": 0,
        "compression_notes": [f"Excluded {excluded} messages outside the reporting window."] if excluded else [],
        "reporting_window": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
    }


def source_receipt(run, provider, source_id, bundle):
    request = run.run_request or {}
    payload = {"version": EXTRACTION_VERSION, "provider": provider, "source_id": source_id,
        "period": {key: request.get(key) for key in ("start_date", "end_date", "backfill_window_start", "backfill_window_end", "draft_months")},
        "startup_context": request.get("startup_context", {}), "bundle": json_value(bundle)}
    for attachment in payload["bundle"].get("attachments", []):
        binary = attachment.pop("raw_content_base64", None)
        if binary:
            attachment["binary_hash"] = content_hash({"base64": binary})
    fingerprint_payload = copy.deepcopy(payload)
    for attachment in fingerprint_payload["bundle"].get("attachments", []):
        for field in ("extracted_text", "extraction_status", "parse_notes", "last_error", "metadata"):
            attachment.pop(field, None)
    return {**payload, "fingerprint": content_hash(fingerprint_payload)}


def stage_source(run, provider, source_id, bundle):
    """Pin a batch before dispatch; completed receipts can be replayed after retries."""
    receipt = source_receipt(run, provider, source_id, bundle)
    key = f"{provider}:{source_id}"
    result = dict(run.result or {})
    evidence = dict(result.get("source_evidence") or {})
    previous = evidence.get(key) or {}
    if previous.get("fingerprint") == receipt["fingerprint"] and previous.get("output") is not None:
        return None
    evidence[key] = receipt
    result["source_evidence"] = evidence
    run.result = result
    # ContentFactoryRun is locked by the caller. Copy prior receipts only within
    # this organization, and only from runs which retained their source evidence.
    from workflow_runs.models import ContentFactoryRun
    from django.db.models.fields.json import KeyTransform
    candidate = ContentFactoryRun.objects.filter(organization_id=run.organization_id, workflow=run.workflow).exclude(pk=run.pk).exclude(status="cancelled").annotate(
        reporting_receipt=KeyTransform(key, KeyTransform("source_evidence", "result")),
    ).filter(reporting_receipt__fingerprint=receipt["fingerprint"]).exclude(
        reporting_receipt__output__isnull=True,
    ).order_by("-id").values_list("reporting_receipt", flat=True).first()
    cached = candidate.get("output") if candidate else None
    return {**bundle, "source_fingerprint": receipt["fingerprint"], "extraction_version": EXTRACTION_VERSION, "cached_extraction": cached}


def complete_source(run, provider, source_id, output):
    key = f"{provider}:{source_id}"
    result = dict(run.result or {})
    evidence = dict(result.get("source_evidence") or {})
    receipt = evidence.get(key)
    if receipt is None:  # Legacy workers have no reusable receipt.
        return
    from rest_framework.exceptions import ValidationError
    if output.get("source_fingerprint") != receipt["fingerprint"]:
        raise ValidationError("Source evidence changed; extract the current source revision.")
    receipt = copy.deepcopy(receipt)
    receipt["output"] = json_value(output)
    parsed = {item["id"]: item for item in receipt["output"].get("attachment_updates", [])}
    for attachment in receipt["bundle"].get("attachments", []):
        if attachment.get("id") in parsed:
            attachment.update(parsed[attachment["id"]])
    evidence[key] = receipt
    result["source_evidence"] = evidence
    run.result = result


def frozen_manual_sources(organization, run):
    from startup_updates.models import StartupManualDocument
    request = run.run_request or {}
    selected = request.get("manual_document_ids") or []
    documents = []
    for document in StartupManualDocument.objects.filter(organization=organization, id__in=selected).order_by("id"):
        payload = {"id": str(document.pk), "filename": document.original_filename,
            "text": document.extracted_text, "status": document.extraction_status,
            "uploaded_at": document.created_at.isoformat(), "parse_notes": document.parse_notes}
        documents.append({**payload, "content_hash": content_hash(payload)})
    return {"summary": str(request.get("manual_summary") or ""), "documents": documents,
        "missing_document_ids": sorted(set(map(str, selected)) - {item["id"] for item in documents})}


def refresh_manual_documents(organization, run):
    from startup_updates.models import StartupManualDocument
    from startup_updates.manual_documents import parse_manual_document
    from core.firebase_utils import download_storage_object_bytes
    warnings = []
    for document in StartupManualDocument.objects.filter(organization=organization, id__in=(run.run_request or {}).get("manual_document_ids", [])):
        if (document.metadata or {}).get("reporting_parser_version") == 2:
            continue
        try:
            raw = download_storage_object_bytes(document.storage_path)
            parsed = parse_manual_document(filename=document.original_filename, content_type=document.content_type, raw_bytes=raw)
            if parsed.extraction_status != "processed":
                warnings.append(f"{document.original_filename}: {parsed.parse_notes}")
                continue
            document.extracted_text = parsed.extracted_text
            document.extraction_status = parsed.extraction_status
            document.text_size_chars = len(parsed.extracted_text)
            document.parse_notes = parsed.parse_notes
            document.metadata = {**(document.metadata or {}), "reporting_parser_version": 2,
                "source_sha256": __import__("hashlib").sha256(raw).hexdigest()}
            document.save(update_fields=["extracted_text", "extraction_status", "text_size_chars", "parse_notes", "metadata", "updated_at"])
        except Exception as exc:
            warnings.append(f"{document.original_filename}: source re-extraction unavailable ({type(exc).__name__}).")
    return warnings
