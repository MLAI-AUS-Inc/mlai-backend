"""Explicit backend fact patches, followed by atomic compatibility projection.

Existing caller permission, original-charge and provider-proof gates remain
mandatory. This service never admits a Run or changes execution lifecycle.
Unmarked historical Jobs retain their original ORM behavior.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from .backend_fact_projection import backend_fact_projection
from .job_mirror import JOB_TO_RUN_FIELDS
from .run_ownership import owns_backend_facts, owner_receipt, reservation_run_id, public_run_value
from .website_contract import sanitized_evidence

BASELINE_ATTRIBUTE = "_run_backend_fact_baseline"
_MISSING = object()


class BackendFactConflict(ValueError):
    """A proposed patch cannot safely update the current canonical facts."""


def capture_fact_baseline(job):
    """Capture loaded metadata only; no database read, admission or persistence."""
    if "request_meta" in job.__dict__:
        setattr(job, BASELINE_ATTRIBUTE, deepcopy(job.__dict__["request_meta"]))


def fact_run_for_job(job_or_id, *, lock=False):
    """Resolve one actual fact owner, including its original long logical key."""
    from workflow_runs.models import ContentFactoryRun
    view_run = getattr(job_or_id, "run", None)
    runs = ContentFactoryRun.objects.select_for_update() if lock else ContentFactoryRun.objects
    if isinstance(view_run, ContentFactoryRun):
        run = runs.filter(pk=view_run.pk).first()
        return run if owns_backend_facts(run) else None
    key = getattr(job_or_id, "job_id", job_or_id)
    if type(key) is not str or not key or len(key) > 255:
        return None
    rows = list(runs.filter(Q(run_id=key) | Q(legacy_job_id=key)
        | Q(result__run_owner_v1__identity__dispatch_key=key)
        | Q(result__run_owner_v1__reservation_run_id=key)).filter(
            result__run_owner_v1__authority__backend_facts=True).order_by("pk")[:2])
    rows = [run for run in rows if owns_backend_facts(run)]
    if len(rows) > 1:
        raise BackendFactConflict("The compatibility identity is ambiguous.")
    return rows[0] if rows else None


def logical_client_request_id(job_or_view):
    """Return original admission identity without truncating a billing key."""
    run = fact_run_for_job(job_or_view)
    if run is not None:
        return owner_receipt(run)["identity"]["dispatch_key"]
    return getattr(job_or_view, "client_request_id", None) or ""


def job_client_key(key):
    """Use a bounded alias only for a known admitted logical key."""
    if type(key) is str and len(key) > 100 and fact_run_for_job(key) is not None:
        return reservation_run_id(key)
    return key


def _metadata(base, proposed, current):
    """Apply only intended nested changes, retaining unrelated concurrent keys."""
    if not all(isinstance(value, dict) for value in (base, proposed, current)):
        raise BackendFactConflict("Metadata needs a captured object baseline.")
    result = deepcopy(current)
    for key in set(base) | set(proposed):
        old, new, actual = (value.get(key, _MISSING) for value in (base, proposed, current))
        if new == old:
            continue
        if isinstance(old, dict) and isinstance(new, dict) and isinstance(actual, dict):
            result[key] = _metadata(old, new, actual)
        elif actual != old and actual != new:
            raise BackendFactConflict("Concurrent metadata changed the same key.")
        elif new is _MISSING:
            result.pop(key, None)
        else:
            result[key] = deepcopy(new)
    return result


def _json(value, *, kind):
    if not isinstance(value, kind):
        raise BackendFactConflict("The fact has an invalid JSON shape.")
    value = sanitized_evidence(public_run_value(deepcopy(value)))
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BackendFactConflict("The fact is not bounded JSON.") from exc
    if len(raw.encode()) > 1_048_576:
        raise BackendFactConflict("The fact exceeds the bounded JSON size.")
    return value


def _merge_delta(current, patch):
    result = deepcopy(current)
    for key, value in patch.items():
        result[key] = (_merge_delta(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value))
    return result


def _validated(fields, run):
    from .models import ContentFactoryJob
    result = {}
    logical = owner_receipt(run)["identity"]["dispatch_key"]
    for name, value in fields.items():
        name = "billing_ledger_id" if name == "billing_ledger" else name
        if name not in JOB_TO_RUN_FIELDS or name == "created_at":
            raise BackendFactConflict("Only explicit mutable backend facts may be patched.")
        if callable(value):
            value = value()
        if name == "updated_at":
            result[name] = timezone.now()
            continue
        if name == "billing_ledger_id":
            value = getattr(value, "pk", value)
            if value is not None and (type(value) is not int or not 0 < value < 2 ** 63):
                raise BackendFactConflict("The billing reference has an invalid type.")
        if name == "client_request_id":
            if value not in (None, "", logical, reservation_run_id(logical)):
                raise BackendFactConflict("The admitted logical key cannot be replaced.")
            value = reservation_run_id(logical)
        if name == "request_meta":
            result[name] = _json(value, kind=dict)
            continue
        if name in {"selection_data", "posted_progress_ids"}:
            result[name] = _json(value, kind=dict if name == "selection_data" else list)
            continue
        field = ContentFactoryJob._meta.get_field("billing_ledger" if name == "billing_ledger_id" else name)
        if value is None and not field.null:
            raise BackendFactConflict("The fact cannot be null.")
        if isinstance(field, models.IntegerField) and value is not None and type(value) is not int:
            raise BackendFactConflict("Integer facts cannot be coerced from another type.")
        if isinstance(field, models.DateTimeField) and value is not None:
            if not isinstance(value, datetime) or not timezone.is_aware(value):
                raise BackendFactConflict("A progress timestamp must be timezone aware.")
        if isinstance(field, (models.CharField, models.TextField)) and value is not None and type(value) is not str:
            raise BackendFactConflict("Text facts cannot be coerced from another type.")
        if isinstance(value, str) and len(value) > (field.max_length or 65_536):
            raise BackendFactConflict("The text fact exceeds its bound.")
        try:
            field.run_validators(value)
        except ValidationError as exc:
            raise BackendFactConflict("The fact does not match its declared field.") from exc
        if name == "status" and value not in dict(ContentFactoryJob.STATUS_CHOICES):
            raise BackendFactConflict("The fact uses an execution status instead of a delivery phase.")
        if name == "domain" and value != run.domain:
            raise BackendFactConflict("The compatibility company identity changed.")
        result[name] = value
    return result


def _project(run):
    """Produce a complete compatibility row from the already locked owner."""
    from .models import ContentFactoryJob
    key = run.legacy_job_id or run.run_id
    job = ContentFactoryJob.objects.select_for_update().filter(job_id=key).first()
    if job is not None and run.job_created_at is None:
        raise BackendFactConflict("An existing historical Job cannot seed admitted facts.")
    values = {name: deepcopy(getattr(run, column)) for name, column in JOB_TO_RUN_FIELDS.items()}
    values["client_request_id"] = reservation_run_id(owner_receipt(run)["identity"]["dispatch_key"])
    values["domain"] = values["domain"] if values["domain"] is not None else run.domain
    values["slack_user_id"] = values["slack_user_id"] if values["slack_user_id"] is not None else ""
    values["status"] = values["status"] or "queued"
    created = job is None
    with backend_fact_projection():
        if created:
            initial = {name: value for name, value in values.items() if name not in {"created_at", "updated_at"}}
            job = ContentFactoryJob.objects.create(job_id=key, **initial)
            clocks = {"job_created_at": job.created_at, "job_updated_at": job.updated_at,
                      "job_domain": job.domain, "job_slack_user_id": job.slack_user_id,
                      "client_request_id": job.client_request_id, "delivery_phase": job.status}
            from workflow_runs.models import ContentFactoryRun
            ContentFactoryRun.objects.filter(pk=run.pk).update(**clocks)
            for name, value in clocks.items():
                setattr(run, name, value)
        else:
            ContentFactoryJob.objects.filter(pk=job.pk).update(**values)
            job.refresh_from_db()
    capture_fact_baseline(job)
    return job, created


def _apply(run, fields, *, metadata_base=_MISSING, metadata_patch=False):
    from workflow_runs.models import ContentFactoryRun
    patch = _validated(fields, run)
    if "request_meta" in patch:
        current = deepcopy(run.job_meta or {})
        if metadata_patch:
            # Factory defaults name intended keys; they never delete unspecified ones.
            patch["request_meta"] = _merge_delta(current, patch["request_meta"])
        else:
            patch["request_meta"] = _metadata(metadata_base, patch["request_meta"], current)
        patch["request_meta"]["backend_client_request_id"] = owner_receipt(run)["identity"]["dispatch_key"]
    values = {JOB_TO_RUN_FIELDS[name]: value for name, value in patch.items()}
    if "backend_client_request_id" not in (run.job_meta or {}) and "job_meta" not in values:
        values["job_meta"] = {**(run.job_meta or {}),
            "backend_client_request_id": owner_receipt(run)["identity"]["dispatch_key"]}
    if values:
        ContentFactoryRun.objects.filter(pk=run.pk).update(**values)
        for name, value in values.items():
            setattr(run, name, value)
    return _project(run)


def save_job_facts(job, *, update_fields=None, legacy_full_save=False):
    """Patch only intended fields on the Run before projecting the Job output."""
    run = fact_run_for_job(job)
    if run is None:
        if legacy_full_save:
            job.save()
        else:
            job.save(update_fields=update_fields)
        return
    if update_fields is None or not update_fields or getattr(job, "pk", None) is None:
        raise BackendFactConflict("Admitted saves require an explicit existing-row patch.")
    fields = {name: getattr(job, name) for name in update_fields}
    baseline = getattr(job, BASELINE_ATTRIBUTE, _MISSING)
    with transaction.atomic():
        run = fact_run_for_job(job, lock=True)
        if run is None:
            raise BackendFactConflict("The canonical fact owner changed.")
        projected, _ = _apply(run, fields, metadata_base=baseline)
        for field in job._meta.concrete_fields:
            setattr(job, field.attname, deepcopy(getattr(projected, field.attname)))
        capture_fact_baseline(job)


def _factory(operation, *, job_id, defaults=None, **fields):
    from .models import ContentFactoryJob
    run = fact_run_for_job(job_id)
    if run is None:
        if operation == "create":
            return ContentFactoryJob.objects.create(job_id=job_id, **fields)
        return getattr(ContentFactoryJob.objects, operation)(job_id=job_id, defaults=defaults)
    with transaction.atomic():
        run = fact_run_for_job(job_id, lock=True)
        if run is None:
            raise BackendFactConflict("The canonical fact owner changed.")
        existing = ContentFactoryJob.objects.filter(job_id=run.legacy_job_id or run.run_id).exists()
        if operation == "create" and existing:
            raise IntegrityError("The compatibility Job already exists.")
        if operation == "get_or_create" and existing:
            return _project(run)[0], False
        patch = fields if operation == "create" else dict(defaults or {})
        projected, created = _apply(run, patch, metadata_patch=True)
        return projected if operation == "create" else (projected, created)


def create_job_facts(**fields):
    """Create compatibility output only after an existing canonical admission."""
    return _factory("create", **fields)


def get_or_create_job_facts(*, job_id, defaults=None):
    """Return existing canonical facts, or create their first compatibility row."""
    return _factory("get_or_create", job_id=job_id, defaults=defaults)


def update_or_create_job_facts(*, job_id, defaults=None):
    """Apply an explicit factory patch without replacing unrelated fact domains."""
    return _factory("update_or_create", job_id=job_id, defaults=defaults)


def update_job_facts(queryset, **fields):
    """Route explicit bulk patches through each existing canonical owner."""
    entries = list(queryset.values_list("pk", "job_id"))
    owned = [(pk, key) for pk, key in entries if fact_run_for_job(key) is not None]
    if not owned:
        return queryset.update(**fields)
    owned_pks, count = {pk for pk, _ in owned}, 0
    with transaction.atomic():
        for pk, key in owned:
            run = fact_run_for_job(key, lock=True)
            if run is None or not queryset.filter(pk=pk).exists():
                continue
            _apply(run, fields, metadata_patch=True)
            count += 1
        count += queryset.exclude(pk__in=owned_pks).update(**fields)
    return count


@contextmanager
def owned_job_transaction(job_or_id):
    """Lock canonical Run before compatibility Job and subsequent billing locks."""
    from .models import ContentFactoryJob
    with transaction.atomic():
        run = fact_run_for_job(job_or_id, lock=True)
        key = (run.legacy_job_id or run.run_id) if run is not None else getattr(job_or_id, "job_id", job_or_id)
        job = ContentFactoryJob.objects.select_for_update().get(job_id=key)
        yield job
