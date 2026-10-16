"""Company-scoped channel planning and owner choices, with no publishing effects."""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .website_channel_observation import canonical_domain, observe_homepage, SIGNALS
from .website_contract import WebsiteAuthorityError

CHANNELS = frozenset({"git", "cms", "hosted", "portable"})
PLATFORMS = frozenset({"wordpress", "ghost", "webflow", "shopify", "wix", "squarespace", "unknown"})
FRAMEWORKS = frozenset({"nextjs", "next", "react", "react_router", "react-router", "vite-react",
    "remix", "vite", "astro", "static_html", "html", "hugo", "static"})
ADAPTERS = frozenset({"next_pages_router", "next_app_router", "react_router", "astro_content",
    "static_markdown", "hook_materialized", "astro"})


def enabled():
    """Keep planning independently disabled until channel handlers are ready."""
    return getattr(settings, "ARTICLES_CHANNEL_DISCOVERY_ENABLED", False)


def scope_for(context, config):
    """Bind public hints to the current company, origin and repository selection."""
    try:
        domain = canonical_domain(context.organization.domain)
        company_domain = canonical_domain(context.company.domain)
    except (ValueError, UnicodeError) as exc:
        raise WebsiteAuthorityError("website_prepare_blocked", "Select a public website hostname.", status=422) from exc
    if (str(context.organization.pk) != str(config.organization_id) or
            context.profile.role != "founder" or
            str(getattr(context.company, "organization_id", "")) != str(config.organization_id) or
            str(context.company.profile_id) != str(context.profile.pk) or
            company_domain != domain):
        raise WebsiteAuthorityError("website_configuration_changed", "Refresh the selected company website.")
    connection = config.website_connection
    return {"company_id": str(context.company.pk), "organization_id": str(config.organization_id),
        "config_id": str(config.pk), "domain": domain,
        "connection_id": str(connection.pk) if connection else None,
        "generation": connection.generation if connection else None,
        "connection_state": connection.state if connection else None,
        "github_repo": connection.github_repo if connection else "",
        "branch": connection.branch if connection else "", "app_root": connection.app_root if connection else ""}


def recommend(observation, *, fingerprint=None, repository_scope=None):
    """Use validated current E1 facts first; all recommendations remain hints."""
    from .website_site_fingerprint import project, framework_hint
    platform = observation.get("platform", "unknown")
    channel, confidence = "hosted", observation.get("confidence", "unknown")
    facts = project(fingerprint, **repository_scope) if repository_scope else None
    adapter = facts["fields"]["adapter"] if facts else {}
    framework = framework_hint(facts) if facts else None
    if (framework in FRAMEWORKS and adapter.get("confidence") == "high" and
            isinstance(adapter.get("value"), str) and adapter["value"] in ADAPTERS):
        channel, confidence = "git", "high"
    elif platform in PLATFORMS - {"unknown", "squarespace"}:
        channel = "cms"
    options = list(dict.fromkeys([channel, *(["git"] if repository_scope else []), "hosted", "portable"]))
    return {"recommended_channel": channel, "platform": platform, "confidence": confidence,
        "options": options, "signals": list(observation["signals"]),
        "observation_status": observation["status"], "coverage": observation["coverage"],
        "prefix_sha256": observation["prefix_sha256"],
        "fingerprint_digest": facts["digest"] if facts else None,
        "source_sha": repository_scope["source_sha"] if repository_scope else None}


def saved_record(article_system):
    """Project a closed local record; repository/worker data cannot author it."""
    raw = article_system.get("publishing_channel") if isinstance(article_system, dict) else None
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "scope", "recommendation", "selection", "observed_at"}:
        return None
    recommendation, selection, scope = raw.get("recommendation"), raw.get("selection"), raw.get("scope")
    if (raw.get("schema_version") != 1 or type(raw.get("schema_version")) is not int or
            type(raw.get("revision")) is not int or not 1 <= raw["revision"] < 10**12 or
            not isinstance(scope, dict) or set(scope) != {"company_id", "organization_id", "config_id", "domain",
                "connection_id", "generation", "connection_state", "github_repo", "branch", "app_root"} or
            not isinstance(recommendation, dict) or set(recommendation) != {"recommended_channel", "platform", "confidence", "options",
                "signals", "observation_status", "coverage", "prefix_sha256", "fingerprint_digest", "source_sha"}):
        return None
    try:
        if (len(json.dumps(raw)) > 8192 or recommendation["recommended_channel"] not in CHANNELS or
                recommendation["platform"] not in PLATFORMS or recommendation["confidence"] not in {"high", "medium", "unknown"} or
                recommendation["observation_status"] not in {"observed", "unavailable"} or recommendation["coverage"] not in {"none", "prefix", "complete"} or
                not isinstance(recommendation["signals"], list) or len(recommendation["signals"]) > len(SIGNALS) or
                any(not isinstance(item, str) or item not in SIGNALS for item in recommendation["signals"]) or
                not isinstance(recommendation["options"], list) or not 2 <= len(recommendation["options"]) <= 4 or
                any(not isinstance(item, str) or item not in CHANNELS for item in recommendation["options"]) or
                not {"hosted", "portable", recommendation["recommended_channel"]} <= set(recommendation["options"]) or
                selection is not None and (not isinstance(selection, dict) or set(selection) != {"channel", "platform"} or
                    selection["channel"] not in CHANNELS or selection["platform"] not in PLATFORMS)):
            return None
        for key in ("prefix_sha256", "fingerprint_digest"):
            value = recommendation[key]
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
                return None
        stamp = timezone.datetime.fromisoformat(raw["observed_at"])
        if timezone.is_naive(stamp) or stamp > timezone.now() + timedelta(minutes=5):
            return None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    return deepcopy(raw)


def preserve_choice(current, incoming):
    """Carry only the existing backend record across untrusted detector writes."""
    incoming = deepcopy(incoming) if isinstance(incoming, dict) else {}
    incoming.pop("publishing_channel", None)
    record = saved_record(current)
    if record:
        incoming["publishing_channel"] = record
    return incoming


def refresh_repository_hint(connection, config, *, source_sha, inventory):
    """Refresh a recommendation within the caller's current consent lock.

    Reuse saved public hints without HTTP. A fresh exact-scope E1 fingerprint
    may improve the recommendation after the initial connect scan; the owner's
    selection and public observation time remain unchanged.
    """
    if not enabled() or config.website_connection_id != connection.pk or config.last_scanned_sha != source_sha:
        return
    record = saved_record(config.article_system)
    if not record:
        return
    saved_scope = record["scope"]
    current_fields = {"organization_id": str(config.organization_id), "config_id": str(config.pk),
        "domain": connection.organization.domain, "connection_id": str(connection.pk),
        "generation": connection.generation,
        "connection_state": connection.state, "github_repo": connection.github_repo,
        "branch": connection.branch, "app_root": connection.app_root}
    if connection.state != "connected" or any(saved_scope.get(key) != value for key, value in current_fields.items()):
        return
    previous = record["recommendation"]
    strong = {signal.split(":")[0] for signal in previous["signals"] if not signal.endswith(":resource")}
    public_confidence = "high" if len(strong) == 1 else "medium" if previous["platform"] != "unknown" else "unknown"
    observation = {"platform": previous["platform"], "confidence": public_confidence,
        "signals": previous["signals"], "status": previous["observation_status"],
        "coverage": previous["coverage"], "prefix_sha256": previous["prefix_sha256"]}
    recommendation = recommend(observation, fingerprint=inventory.get("site_fingerprint"),
        repository_scope={"github_repo": connection.github_repo, "source_sha": source_sha,
            "branch": connection.branch, "app_root": connection.app_root})
    if recommendation == previous:
        return
    record.update(recommendation=recommendation, revision=record["revision"] + 1)
    config.article_system = {**config.article_system, "publishing_channel": record}
    config.save(update_fields=["article_system", "updated_at"])


def projection(context, config):
    """Status is a local read; it never fetches, schedules or grants readiness."""
    if not enabled():
        return None
    try:
        scope = scope_for(context, config)
    except (ValueError, UnicodeError, WebsiteAuthorityError):
        return None
    record = saved_record(config.article_system)
    return record if record and record["scope"] == scope else {"schema_version": 1, "revision": _revision(config),
        "scope": scope, "recommendation": None, "selection": None, "observed_at": None}


def _revision(config):
    record = saved_record(config.article_system)
    return record["revision"] if record else 0


def _require_current(context, config, expected_scope, expected_revision):
    if not enabled():
        raise WebsiteAuthorityError("website_prepare_blocked", "Channel discovery is not available.", status=404)
    if (type(expected_revision) is not int or expected_revision != _revision(config) or
            scope_for(context, config) != expected_scope):
        raise WebsiteAuthorityError("website_configuration_changed", "Refresh the website channel choices before continuing.")


def update_choice(context, config, *, expected_scope, expected_revision, selected=None):
    """Inspect outside locks, then save after exact company/site/revision checks.

    ``selected`` records an owner preference only. No CMS, hosting, GitHub,
    generation, billing or publishing authority is created by this action.
    """
    if not enabled():
        raise WebsiteAuthorityError("website_prepare_blocked", "Channel discovery is not available.", status=404)
    from organizations.models import Organization
    from founder_tools.models import VibeRaisingCompany
    scope = expected_scope
    _require_current(context, config, scope, expected_revision)
    observed_source = str(config.last_scanned_sha or "")
    observed_version = config.website_connection.configuration_version if config.website_connection else None
    current = saved_record(config.article_system)
    if selected is None:
        observation = observe_homepage(scope["domain"])
        connection = config.website_connection
        repository_scope = None
        fingerprint = None
        if connection and connection.state == "connected" and observed_source:
            from .website_site_fingerprint import for_connection
            fingerprint = for_connection(connection, observed_source)
            repository_scope = {"github_repo": connection.github_repo, "source_sha": observed_source,
                "branch": connection.branch, "app_root": connection.app_root}
        recommendation = recommend(observation, fingerprint=fingerprint, repository_scope=repository_scope)
        record = {"schema_version": 1, "revision": expected_revision + 1, "scope": scope,
            "recommendation": recommendation, "selection": current["selection"] if current and current["scope"] == scope else None,
            "observed_at": timezone.now().isoformat()}
    else:
        if (not isinstance(selected, str) or selected not in CHANNELS or not current or current["scope"] != scope or
                selected not in current["recommendation"]["options"]):
            raise WebsiteAuthorityError("website_prepare_blocked", "Inspect this website and choose an offered channel.", status=422)
        record = {**current, "revision": expected_revision + 1,
            "selection": {"channel": selected, "platform": current["recommendation"]["platform"] if selected == "cms" else "unknown"}}
    with transaction.atomic():
        # Same serialization order as repository bind, config PUT and revocation.
        Organization.objects.select_for_update().get(pk=config.organization_id)
        context.organization.refresh_from_db()
        context.profile.refresh_from_db()
        company = VibeRaisingCompany.objects.select_for_update().get(pk=context.company.pk)
        locked_context = SimpleNamespace(organization=context.organization, company=company, profile=context.profile)
        config.refresh_from_db()
        _require_current(locked_context, config, scope, expected_revision)
        current_version = config.website_connection.configuration_version if config.website_connection else None
        if selected is None and (str(config.last_scanned_sha or "") != observed_source or current_version != observed_version):
            raise WebsiteAuthorityError("website_configuration_changed", "Refresh channel discovery for the current repository source.")
        config.article_system = {**(config.article_system if isinstance(config.article_system, dict) else {}), "publishing_channel": record}
        config.save(update_fields=["article_system", "updated_at"])
    return deepcopy(record)
