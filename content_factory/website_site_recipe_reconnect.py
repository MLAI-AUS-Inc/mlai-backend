"""Retain accepted site data across an explicit reconnect, never old build proof."""
from copy import deepcopy
import json
from types import SimpleNamespace

from django.conf import settings
from django.utils import timezone

from . import website_goal as goals
from .website_failure_redaction import redact_secret_text
from .website_site_recipe import dispatch_selection
from .website_target_contract import normalize_native_metadata

_FIELDS = ("target_id", "kind", "delivery_adapter", "content_path_pattern", "route_template",
    "registration_strategy", "metadata_strategy", "publish_capability", "content_extension",
    "preview_strategy", "confidence", "adapter_version", "site_contract_version",
    "site_contract_digest", "site_verification_probe", "contract_digest")
_SCOPE = ("repository_id", "github_repo", "branch", "app_root", "site_url")


def enabled(connection):
    """Reconnect remains behind both services' existing independent rollout gates."""
    from .website_hosted_verification import enabled as hosted_enabled
    return getattr(settings, "ARTICLES_SITE_CONTRACT_V2_ENABLED", False) and hosted_enabled(connection)


def certified_definition(connection, row):
    """Return a closed site descriptor only from a previously certified native row."""
    try:
        if (str(row.connection_id) != str(connection.pk) or type(row.generation) is not int
                or row.generation > connection.generation or not row.verified_at
                or row.capabilities.get("publishingReady") is not True
                or not isinstance(row.source_sha, str) or not goals.SHA.fullmatch(row.source_sha)):
            return None
        stored = normalize_native_metadata(row.contract)
        proof = goals.mapping(stored.get("verification"))
        if (proof.get("status") not in {"passed", "verified"} or proof.get("source_sha") != row.source_sha
                or proof.get("website_connection_id") != str(connection.pk)
                or type(proof.get("connection_generation")) is not int or proof["connection_generation"] != row.generation
                or type(proof.get("repository_id")) is not int or proof["repository_id"] != connection.repository_id
                or stored.get("target_id") != row.target_key or stored.get("site_contract_version") != 2):
            return None
        definition = {key: deepcopy(stored[key]) for key in _FIELDS if key in stored}
        marker = goals.mapping(stored.get("live_marker"))
        definition["contract"] = {"live_marker": {key: deepcopy(marker[key]) for key in (
            "kind", "value", "meta_name", "schema_version") if key in marker}, "contract_digest": stored["contract_digest"]}
        # This is explicitly a definition projection, not a current target or a
        # source/proof projection. Selection validates the retained data shape.
        candidate = SimpleNamespace(connection_id=connection.pk, generation=connection.generation,
            target_key=row.target_key, contract=definition)
        if dispatch_selection(connection, candidate, row.source_sha) is None:
            return None
        raw = json.dumps(definition, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 32768 or redact_secret_text(raw) != raw:
            return None
        return definition
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def candidate_for(connection, config):
    """Read the selected or sole latest certified definition under the bind lock."""
    if not enabled(connection):
        return None
    rows = connection.targets.filter(contract__site_contract_version=2, verified_at__isnull=False,
        capabilities__publishingReady=True)
    if config.default_publish_target_id:
        rows = rows.filter(target_key=config.default_publish_target_id)
    rows = list(rows.order_by("-generation", "-updated_at", "-pk")[:2])
    if not rows or len(rows) > 1 and rows[0].generation == rows[1].generation:
        return None
    row = rows[0]
    definition = certified_definition(connection, row)
    if definition is None:
        return None
    return {"definition": definition, "source_sha": row.source_sha, "previous_target_id": str(row.pk),
        "previous_generation": row.generation, "scope": {key: getattr(connection, key) for key in _SCOPE}}


def enroll_after_bind(config, connection, candidate, *, user):
    """Reserve fresh provisional data and durable verification after same-site bind."""
    from .website_connections import contract_for
    from .website_models import WebsiteConnectionTarget, WebsiteConnectionOperation
    from .website_operations import deletion_epoch
    if (not candidate or not enabled(connection) or connection.state != "connected"
            or str(connection.authorized_by_id) != str(user.pk)
            or candidate["scope"] != {key: getattr(connection, key) for key in _SCOPE}
            or candidate["previous_generation"] >= connection.generation):
        return None
    definition = deepcopy(candidate["definition"])
    definition["provisional"] = True
    WebsiteConnectionTarget.objects.get_or_create(connection=connection, generation=connection.generation,
        target_key=definition["target_id"], defaults={"adapter": definition["delivery_adapter"], "adapter_version": "1",
            "source_sha": "", "verified_at": None, "capabilities": {"publishingReady": False}, "contract": definition})
    config.publish_targets = [definition]
    config.default_publish_target_id = definition["target_id"]
    config.save(update_fields=["publish_targets", "default_publish_target_id", "updated_at"])
    op, _ = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{connection.pk}:site-recipe-reconnect:{connection.generation}", defaults={
            "connection": connection, "generation": connection.generation, "action": "source-reverify", "state": "pending",
            "next_attempt_at": timezone.now(), "payload": {**contract_for(connection), "attempt": 1,
                "site_recipe_reconnect": True, "requested_by_user_id": str(user.pk),
                "deletion_epoch": deletion_epoch(connection), "source_sha": candidate["source_sha"],
                "target_id": definition["target_id"], "contract_digest": definition["contract_digest"],
                "site_contract_digest": definition["site_contract_digest"],
                "previous_target_id": candidate["previous_target_id"], "previous_generation": candidate["previous_generation"],
                "scope": deepcopy(candidate["scope"]), "scan_required": False},
            "receipt": {"status": "verification_pending", "current_step": "verify", "repository_modified": False,
                "message": "Verifying the existing articles setup for this connection."}})
    return op


def trusted_source_review(connection, origin, data):
    """Recognize the exact prior reviewed source; fresh hosted proof is still required."""
    from .website_operations import deletion_epoch
    reference = goals.mapping(getattr(origin, "payload", None)).get("site_recipe_reconnect_operation_id")
    if not reference or not enabled(connection):
        return False
    op = connection.operations.filter(pk=reference, action="source-reverify", generation=connection.generation,
        state="pending", payload__site_recipe_reconnect=True).first()
    payload = goals.mapping(getattr(op, "payload", None))
    binding = goals.mapping(origin.payload.get("binding"))
    if (not op or op.generation != connection.generation or connection.state != "connected"
            or payload.get("requested_by_user_id") != str(connection.authorized_by_id)
            or payload.get("scope") != {key: getattr(connection, key) for key in _SCOPE}
            or type(payload.get("deletion_epoch")) is not int or payload["deletion_epoch"] != deletion_epoch(connection)
            or type(payload.get("previous_generation")) is not int or payload["previous_generation"] >= connection.generation
            or type(binding.get("site_recipe_version")) is not int or binding["site_recipe_version"] != 2
            or origin.generation != connection.generation or origin.state != "completed"
            or binding.get("run_id") != data.get("run_id") or binding.get("expected_source_sha") != data.get("source_sha")
            or any(payload.get(key) != data.get(key) for key in ("source_sha", "target_id", "contract_digest"))
            or payload.get("site_contract_digest") != binding.get("site_contract_digest")):
        return False
    prior = connection.targets.filter(pk=payload.get("previous_target_id"), generation=payload["previous_generation"]).first()
    definition = certified_definition(connection, prior) if prior else None
    return bool(definition and prior.source_sha == data.get("source_sha")
        and definition["target_id"] == data.get("target_id")
        and definition["contract_digest"] == data.get("contract_digest")
        and definition["site_contract_digest"] == binding.get("site_contract_digest"))
