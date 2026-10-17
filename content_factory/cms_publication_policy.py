"""Owner-confirmed native publication choices, independent of Git preferences."""

from copy import deepcopy
import re
from uuid import UUID

from django.db import transaction

from .channel_artifact_contract import canonical_bytes, digest
from .cms_api_transport import CmsApiError, request_json
from .cms_connection_authority import current_credential, validate_current, enabled
from .cms_connection_contract import validate_binding, CmsContractError
from .cms_owner_connection import _owner, _request_id
from .website_contract import WebsiteAuthorityError, connection_contract

ACTION = "cms-policy"
REFERENCE = "_cms_publication_policy_id"
FIELDS = {"schema_version", "kind", "binding", "credential_operation_id", "review_policy", "publication_confirmed",
    "native_defaults_confirmed", "native_template", "labels", "request_digest", "digest"}


def _deny():
    return WebsiteAuthorityError("website_configuration_changed", "The current CMS publication choices or owner changed.")


def _identifier(value, platform):
    if platform == "wordpress":
        return type(value) is int and 0 < value < 10**19
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{24}", value) is not None


def validate_policy(value):
    """A closed owner choice is data; only its private current operation is consent."""
    try:
        if (not isinstance(value, dict) or set(value) != FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                value["kind"] != "cms_publication_policy" or value["review_policy"] not in {"auto", "review"} or
                value["publication_confirmed"] is not True or value["native_defaults_confirmed"] is not True or
                not isinstance(value["credential_operation_id"], str) or str(UUID(value["credential_operation_id"])) != value["credential_operation_id"] or
                any(not isinstance(value[key], str) or re.fullmatch(r"[a-f0-9]{64}", value[key]) is None for key in ("request_digest", "digest"))):
            raise _deny()
        binding = validate_binding(value["binding"])
        template = value["native_template"]
        if not isinstance(template, dict) or template.get("hero") != "inline_only":
            raise _deny()
        if binding["platform"] == "wordpress":
            if (set(template) != {"author", "categories", "tags", "hero"} or
                    not _identifier(template["author"], "wordpress") or not template["categories"]):
                raise _deny()
            keys = ("categories", "tags")
        else:
            if set(template) != {"authors", "tags", "hero"} or not template["authors"]:
                raise _deny()
            keys = ("authors", "tags")
        for key in keys:
            if (not isinstance(template[key], list) or len(template[key]) > 100 or len(set(template[key])) != len(template[key]) or
                    any(not _identifier(item, binding["platform"]) for item in template[key])):
                raise _deny()
        labels = value["labels"]
        if (not isinstance(labels, dict) or set(labels) != {"author", "category"} or
                any(not isinstance(label, str) or len(label) > 200 or any(ord(c) < 32 for c in label) for label in labels.values()) or
                value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise _deny()
        return deepcopy(value)
    except WebsiteAuthorityError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def current_policy(context, config, *, user):
    """Resolve only the exact currently selected private owner consent record."""
    binding = validate_current(context, config, config.website_connection, user=user)
    reference = config.website_connection.capabilities.get(REFERENCE)
    if reference is None:
        return None
    try:
        if not isinstance(reference, str) or str(UUID(reference)) != reference:
            raise _deny()
        operation = config.website_connection.operations.filter(pk=reference, action=ACTION,
            generation=binding["generation"], state="completed").first()
        policy = validate_policy(operation.payload) if operation else None
        if (operation is None or operation.action != ACTION or operation.state != "completed" or
                operation.generation != binding["generation"] or str(operation.connection_id) != binding["connection_id"] or
                policy["binding"] != binding or
                policy["credential_operation_id"] != config.website_connection.capabilities.get("_cms_credential_operation_id")):
            raise _deny()
        return {"operation_id": str(operation.pk), "policy": policy}
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _deny() from None


def _label(value, fallback):
    return value if isinstance(value, str) and 0 < len(value) <= 200 and not any(ord(c) < 32 for c in value) else fallback


def resolve_native_defaults(read, platform):
    """Read actual native identifiers; never assume a default category ID.

    The callback refreshes private scope before each fixed provider request.
    No author, category, tag, account or post is created by this discovery.
    """
    if platform == "wordpress":
        user = read(resource="users", identifier="me", query={"context": "edit"})
        if (not isinstance(user, dict) or not _identifier(user.get("id"), platform) or
                not isinstance(user.get("capabilities"), dict) or
                user["capabilities"].get("edit_posts") is not True or user["capabilities"].get("publish_posts") is not True):
            raise CmsApiError("native_defaults_unavailable")
        terms = read(resource="categories", query={"per_page": 1, "orderby": "id", "order": "asc", "hide_empty": "false"})
        if (not isinstance(terms, list) or len(terms) != 1 or not isinstance(terms[0], dict) or
                not _identifier(terms[0].get("id"), platform) or terms[0].get("taxonomy") != "category"):
            raise CmsApiError("native_defaults_unavailable")
        return ({"author": user["id"], "categories": [terms[0]["id"]], "tags": [], "hero": "inline_only"},
            {"author": _label(user.get("name"), "Connected CMS author"), "category": _label(terms[0].get("name"), "Existing category")})
    rows = read(resource="users", query={"limit": 100, "filter": "status:active", "order": "id asc"})
    users = rows.get("users") if isinstance(rows, dict) else None
    if not isinstance(users, list) or not 0 < len(users) <= 100:
        raise CmsApiError("native_defaults_unavailable")
    author = users[0]
    if (not isinstance(author, dict) or not _identifier(author.get("id"), platform) or author.get("status") != "active"):
        raise CmsApiError("native_defaults_unavailable")
    return ({"authors": [author["id"]], "tags": [], "hero": "inline_only"},
        {"author": _label(author.get("name"), "Existing CMS author"), "category": ""})


def configure(context, config, *, user, data, setup_scope=None):
    """Record the setup owner's exact review/auto choice after scoped native reads."""
    if not enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS publication setup is not available.", status=404)
    request_id = _request_id(data)
    if (not isinstance(data.get("review_policy"), str) or data["review_policy"] not in {"auto", "review"} or
            data.get("publication_confirmed") is not True or data.get("native_defaults_confirmed") is not True):
        raise WebsiteAuthorityError("website_prepare_blocked", "Choose review or automatic publication and confirm the CMS publishing settings.", status=422)
    request_digest = digest({key: data[key] for key in ("review_policy", "publication_confirmed", "native_defaults_confirmed")})
    config.refresh_from_db()
    original_reference = (config.website_connection.capabilities or {}).get(REFERENCE) if config.website_connection else None
    expected_reference = data.get("expected_policy_id")
    def setup_guard():
        if setup_scope is not None:
            from .cms_setup_authority import for_policy
            for_policy(setup_scope, context=context, config=config, user=user, data=data)
    def guard():
        setup_guard()
        for value in (context.organization, context.profile, context.company, user, config): value.refresh_from_db()
        _owner(context, config, user)
        binding, credential = current_credential(context, config, config.website_connection, user=user)
        expected = connection_contract(data)
        if (expected.get("website_connection_id") != binding["connection_id"] or expected.get("connection_generation") != binding["generation"] or
                expected.get("repository_id") is not None or expected.get("connection_target_id") or
                (config.website_connection.capabilities or {}).get(REFERENCE) != original_reference):
            raise _deny()
        return binding, credential
    binding, credential = guard()
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    key = "cms-policy:" + binding["connection_id"] + ":" + str(binding["generation"]) + ":" + request_id
    def replay(prior):
        setup_guard()
        for value in (context.organization, context.profile, context.company, user, config): value.refresh_from_db()
        _owner(context, config, user)
        saved = current_policy(context, config, user=user)
        if (saved is None or saved["operation_id"] != str(prior.pk) or saved["policy"]["request_digest"] != request_digest or
                saved["policy"]["binding"] != binding):
            raise _deny()
        return saved
    prior = WebsiteConnectionOperation.objects.filter(idempotency_key=key, action=ACTION).first()
    if prior is not None:
        return replay(prior)
    if expected_reference != original_reference:
        raise _deny()
    def read(**options):
        fresh, secret = guard()
        if fresh != binding:
            raise _deny()
        from .website_connections import require_unlocked_remote_call
        require_unlocked_remote_call()
        if setup_scope is not None:
            from .cms_setup_controller import reserve_provider_read
            reserve_provider_read(setup_scope)
        result, _pagination = request_json(fresh, secret, method="GET", **options)
        return result
    try:
        template, labels = resolve_native_defaults(read, binding["platform"])
    except CmsApiError as exc:
        if exc.category in {"transient", "origin_unavailable"}:
            raise WebsiteAuthorityError("website_prepare_dependency_transient", "The CMS discovery service is temporarily unavailable.",
                retryable=True, status=503) from None
        raise WebsiteAuthorityError("missing_or_expired_credentials" if exc.category == "credentials_required" else "publishing_adapter_required",
            "The CMS author or category could not be verified for publishing.") from None
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        # A duplicate request can finish during the read-only provider calls.
        # Reuse that exact current acknowledgement instead of asking the owner
        # to repeat the same setup choice after an avoidable conflict.
        prior = WebsiteConnectionOperation.objects.filter(idempotency_key=key, action=ACTION).first()
        if prior is not None:
            return replay(prior)
        fresh, _credential = guard()
        if fresh != binding:
            raise _deny()
        value = {"schema_version": 1, "kind": "cms_publication_policy", "binding": binding,
            "credential_operation_id": config.website_connection.capabilities["_cms_credential_operation_id"],
            "review_policy": data["review_policy"], "publication_confirmed": True, "native_defaults_confirmed": True,
            "native_template": template, "labels": labels, "request_digest": request_digest}
        value["digest"] = digest(value)
        value = validate_policy(value)
        operation, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
            "action": ACTION, "connection": config.website_connection, "generation": binding["generation"], "state": "completed",
            "payload": value, "receipt": {"status": "publication_choices_saved", "provider_modified": False}})
        if canonical_bytes(operation.payload) != canonical_bytes(value) or operation.state != "completed":
            raise _deny()
        config.website_connection.capabilities = {**config.website_connection.capabilities, REFERENCE: str(operation.pk)}
        config.website_connection.configuration_version += 1
        config.website_connection.save(update_fields=["capabilities", "configuration_version", "updated_at"])
        return {"operation_id": str(operation.pk), "policy": value}


def projection(saved):
    """Return only owner-facing choices, with no API origin or credential reference."""
    policy = saved["policy"]
    return {"id": saved["operation_id"], "reviewPolicy": policy["review_policy"],
        "author": policy["labels"]["author"], "category": policy["labels"]["category"], "imagePlacement": "article"}


def for_article(source):
    """Require the private native consent sealed before this paid generation."""
    scope = source.admitted.scope
    saved = current_policy(scope.context, scope.config, user=scope.user)
    expected = source.admitted.operation.payload["policy"].get("cms_publication_policy")
    if (saved is None or expected != {"operation_id": saved["operation_id"], "digest": saved["policy"]["digest"]} or
            saved["policy"]["review_policy"] != source.handoff["envelope"]["review_policy"]):
        raise _deny()
    return saved


def native_post_fields(policy):
    """Keep reviewed images inside the body, avoiding a duplicate theme hero."""
    policy = validate_policy(policy)
    template = policy["native_template"]
    if policy["binding"]["platform"] == "wordpress":
        return {"author": template["author"], "categories": deepcopy(template["categories"]),
            "tags": deepcopy(template["tags"]), "featured_media": 0}
    return {"authors": deepcopy(template["authors"]), "tags": deepcopy(template["tags"]), "feature_image": None}
