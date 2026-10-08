"""Pure guards for immutable certified website and backend-owned run evidence."""
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .website_contract import WebsiteAuthorityError, evidence_digest, template_validation


PROOF_STATUSES = frozenset({"passed", "verified", "preview_verified"})
NO_DELIVERY_REFUND_CODES = frozenset({
    "INTERNAL_ERROR", "UNCLASSIFIED", "RUN_BUDGET_EXHAUSTED", "ONBOARDING_MODEL_BUDGET_EXHAUSTED",
    "EDITORIAL_REJECTED", "EXPORT_EDITORIAL_REJECTED", "DELIVERY_REVIEW_DRIFT", "SOURCE_CHANGED",
    "ARTICLE_DIRECTORY_AGENT_PROVIDER_ERROR",
})


def proof_stamp(proof, *, now=None):
    """Accept explicit proof time only when it is aware and plausibly current."""
    now = now or timezone.now()
    raw = (proof or {}).get("checked_at") or (proof or {}).get("verified_at")
    stamp = parse_datetime(raw) if isinstance(raw, str) else raw
    if stamp is None or timezone.is_naive(stamp) or stamp > now + timedelta(minutes=5):
        return None
    return stamp


def target_update_allowed(previous, incoming, *, generation, sha):
    """A weaker scan cannot overwrite an exact-generation certified target."""
    if previous is None:
        return True
    if previous.generation != generation:
        raise WebsiteAuthorityError("website_target_generation_migration_required",
            "This target belongs to an older generation. The reviewed target-key migration is required.")
    if not previous.verified_at or previous.source_sha != sha:
        return True
    proof = incoming.get("verification") if isinstance(incoming.get("verification"), dict) else {}
    accepted = (previous.contract or {}).get("verification") or {}
    preview_only = accepted.get("status") == "preview_verified" and not previous.capabilities.get("publishingReady")
    statuses = PROOF_STATUSES if preview_only else {"passed", "verified"}
    if proof.get("status") not in statuses or proof.get("source_sha") != sha:
        return False
    if evidence_digest(incoming) == evidence_digest(previous.contract):
        return True
    stamp = proof_stamp(proof)
    return bool(stamp and stamp >= previous.verified_at
        and incoming.get("delivery_adapter") == previous.adapter
        and (preview_only or incoming.get("publish_capability") in {"direct", "hook"}))


def protect_certified_config(config, defaults, *, sha=""):
    """Keep selected certified contracts until an equal or newer proof replaces them."""
    connection = getattr(config, "website_connection", None) if config else None
    if connection is None:
        return defaults
    incoming = defaults.get("publish_targets")
    if not isinstance(incoming, list):
        incoming = list(getattr(config, "publish_targets", None) or [])
    by_key = {str(item.get("target_id") or item.get("id")): item for item in incoming if isinstance(item, dict)}
    certified = list(connection.targets.filter(generation=connection.generation,
        source_sha=connection.verified_sha, verified_at__isnull=False))
    for row in certified:
        candidate = by_key.get(row.target_key)
        if candidate is None or not target_update_allowed(row, candidate, generation=connection.generation, sha=connection.verified_sha):
            by_key[row.target_key] = row.contract
    selected = str(getattr(config, "default_publish_target_id", "") or "")
    if any(row.target_key == selected for row in certified):
        proposed = str(defaults.get("default_publish_target_id") or selected)
        replacement = by_key.get(proposed) or {}
        proof = replacement.get("verification") or {}
        replacement_stamp = proof_stamp(proof)
        selected_stamp = next(row.verified_at for row in certified if row.target_key == selected)
        if proposed != selected and not (proof.get("status") in {"passed", "verified"}
                and proof.get("source_sha") == connection.verified_sha
                and replacement_stamp and replacement_stamp >= selected_stamp):
            defaults["default_publish_target_id"] = selected
    if "publish_targets" in defaults:
        defaults["publish_targets"] = list(by_key.values())
    return defaults


def safe_template_seed(body):
    """Expose quarantined seed status while withholding the rejected envelope."""
    validation = template_validation(body)
    return body if validation["valid"] else None, validation


def delivered_content(run):
    """Conservatively detect persisted delivery before granting no-value refunds."""
    result = getattr(run, "result", None) or {}
    acceptance = getattr(run, "acceptance_summary", None) or {}
    package = result.get("content_package") or result.get("contentPackage") or {}
    package = package if isinstance(package, dict) else {}
    return bool(acceptance.get("content_packaged") or package.get("content_packaged")
        or package.get("contentPackaged") or any(result.get(key) for key in
            ("article", "content", "markdown", "pr_url", "publish_url", "live_url")))


def publish_child_binding(config, source_run, payload, remote_data, *, reviewed_source=None):
    """A publishing child inherits the current verified contract and original scope."""
    from .website_contract import connection_contract
    connection = getattr(config, "website_connection", None)
    if connection is None:
        raise WebsiteAuthorityError("website_connection_required", "Reconnect and verify this website before publishing.")
    original = connection_contract(getattr(source_run, "run_request", None) or {})
    if (not original or original.get("website_connection_id") != str(connection.pk)
            or original.get("connection_generation") != connection.generation
            or str(getattr(source_run, "github_repo", "") or "").casefold() != connection.github_repo.casefold()):
        raise WebsiteAuthorityError("website_repository_changed", "This saved draft belongs to an older website repository. Start a newly reviewed publishing draft.")
    original_request = getattr(source_run, "run_request", None) or {}
    if original_request.get("expected_source_sha") and original_request["expected_source_sha"] != connection.verified_sha:
        raise WebsiteAuthorityError("website_source_changed", "The verified website source changed since this draft. Review a new publishing revision.")
    target = connection.targets.filter(generation=connection.generation,
        target_key=getattr(config, "default_publish_target_id", None), source_sha=connection.verified_sha,
        verified_at__isnull=False).first()
    if target is None or not target.capabilities.get("publishingReady"):
        raise WebsiteAuthorityError("website_target_verification_required", "Select and verify the current article target.")
    remote_repo = remote_data.get("github_repo") or remote_data.get("githubRepo")
    if remote_repo and str(remote_repo).casefold() != connection.github_repo.casefold():
        raise WebsiteAuthorityError("website_repository_changed", "The publishing child targets another repository.")
    remote_sha = remote_data.get("expected_source_sha") or remote_data.get("source_sha")
    if remote_sha and remote_sha != connection.verified_sha:
        raise WebsiteAuthorityError("website_source_changed", "The publishing child uses a different verified website source.")
    proposed_target = payload.get("publish_target_id") or payload.get("connection_target_id")
    if proposed_target and proposed_target != target.target_key:
        raise WebsiteAuthorityError("website_target_changed", "The article publishing target changed.")
    source = getattr(source_run, "result", None) or {}
    article = source.get("article") if isinstance(source.get("article"), dict) else {}
    request = getattr(source_run, "run_request", None) or {}
    package = source.get("delivery_package") or source.get("deliveryPackage") or source.get("content_package") or source.get("contentPackage") or {}
    package = package if isinstance(package, dict) else {}
    article_meta = source.get("article_meta") or source.get("articleMeta") or package.get("article_meta") or {}
    article_meta = article_meta if isinstance(article_meta, dict) else {}
    acceptance = getattr(source_run, "acceptance_summary", None) or {}
    evidence = acceptance.get("evidence_summary") if isinstance(acceptance.get("evidence_summary"), dict) else {}
    slug = package.get("slug") or article_meta.get("slug") or evidence.get("content_package_slug") or source.get("slug") or source.get("article_slug") or article.get("slug") or request.get("article_slug") or request.get("slug")
    if reviewed_source is not None:
        reviewed_request = getattr(reviewed_source, "run_request", None) or {}
        scope_fields = ("expected_source_sha", "operation_id", "operation_attempt", "deletion_epoch")
        if (request.get("source_run_id") != getattr(reviewed_source, "run_id", None)
                or connection_contract(reviewed_request) != original
                or any(request.get(key) != reviewed_request.get(key) for key in scope_fields)
                or getattr(source_run, "organization_id", None) != getattr(reviewed_source, "organization_id", None)):
            raise WebsiteAuthorityError("website_source_changed", "The publishing child does not match its reviewed source and original scope.")
        # Restored children receive article metadata after their first callback.
        # The reviewed parent remains authoritative for this exact child only.
        reviewed_binding = publish_child_binding(config, reviewed_source, payload, remote_data)
        reviewed_slug = reviewed_binding["article_slug"]
        if slug and slug != reviewed_slug:
            raise WebsiteAuthorityError("capture_target_mismatch", "The publishing child slug differs from the saved draft.")
        slug = reviewed_slug
    route = remote_data.get("route_path") or remote_data.get("public_path")
    template = str(target.contract.get("route_template") or "")
    if "{slug}" in template and (not isinstance(slug, str) or not slug.strip()):
        raise WebsiteAuthorityError("capture_target_mismatch", "This saved draft has no confirmed article slug. Review a new publishing revision.")
    remote_slug = remote_data.get("slug") or remote_data.get("article_slug")
    if remote_slug and remote_slug != slug:
        raise WebsiteAuthorityError("capture_target_mismatch", "The publishing child slug differs from the saved draft.")
    if route and template:
        import re
        expected = template.replace("{slug}", str(slug)) if slug else template
        pattern = re.escape(expected).replace(r"\{slug\}", r"[A-Za-z0-9][A-Za-z0-9_-]*")
        if not re.fullmatch(pattern, str(route)):
            raise WebsiteAuthorityError("capture_target_mismatch", "The publishing child route differs from this saved draft and the current verified target.")
    return {**original, "repository_id": connection.repository_id, "connection_target_id": target.target_key,
        "publish_target_id": target.target_key, "expected_source_sha": connection.verified_sha,
        "github_repo": connection.github_repo, "article_slug": slug, "route_path": template.replace("{slug}", slug) if slug else template,
        "publish_target": target.contract}
