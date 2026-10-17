"""Initial owner CMS display choices; no key, connection or money is accepted."""

from copy import deepcopy
from datetime import timedelta
import re
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .channel_artifact_contract import canonical_bytes, digest
from .cms_connection_contract import canonical_origin
from .cms_goal_contract import _time
from .cms_owner_connection import _owner
from .cms_setup_authority import enabled as setup_enabled
from .website_channel_observation import canonical_domain
from .website_contract import WebsiteAuthorityError
from .website_publishing_channels import _require_current, saved_record

TTL = timedelta(minutes=15)
INPUT_FIELDS = {"platform", "public_origin", "api_origin", "channel_scope", "channel_revision"}
SCOPE_FIELDS = {"company_id", "organization_id", "config_id", "domain", "connection_id", "generation", "connection_state", "github_repo", "branch", "app_root"}
FIELDS = {"version", "kind", "scope", "channel_revision", "platform", "public_origin", "api_origin", "first_article_points",
    "setup_points", "target", "image_placement", "quoted_at", "expires_at", "digest"}


def _deny():
    return WebsiteAuthorityError("website_prepare_blocked", "Refresh the current CMS destination, startup and first-article price.", status=422)


def entry_for_context(context, config, *, user):
    """Advertise an initial owner form without selecting a channel or quoting money."""
    from .website_publishing_channels import enabled as discovery_enabled, _revision
    if (user is None or not getattr(settings, "ARTICLES_CMS_ONBOARDING_ENABLED", False) or
            not getattr(settings, "ARTICLES_CMS_STATUS_ENABLED", False) or not discovery_enabled() or
            not setup_enabled(context.organization.pk)):
        return None
    try:
        def current():
            for item in (context.organization, context.profile, context.company, user, config): item.refresh_from_db()
            if getattr(config, "github_repo", None) not in (None, ""): raise _deny()
            return _scope(_owner(context, config, user)), _revision(config)
        scope, revision = current()
        if current() != (scope, revision): return None
        return {"version": 1, "kind": "cms_onboarding_entry", "scope": scope, "channel_revision": revision}
    except (WebsiteAuthorityError, ObjectDoesNotExist, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def _scope(value):
    if (not isinstance(value, dict) or set(value) != SCOPE_FIELDS or not isinstance(value["company_id"], str) or
            str(UUID(value["company_id"])) != value["company_id"] or
            any(not isinstance(value[key], str) or re.fullmatch(r"[1-9][0-9]{0,18}", value[key]) is None
                for key in ("organization_id", "config_id")) or canonical_domain(value["domain"]) != value["domain"] or
            any(value[key] is not None for key in ("connection_id", "generation", "connection_state")) or
            any(value[key] != "" for key in ("github_repo", "branch", "app_root"))):
        raise _deny()
    return deepcopy(value)


def _destinations(scope, platform, public_origin, api_origin):
    public, api = canonical_origin(public_origin), canonical_origin(api_origin)
    if (platform not in {"wordpress", "ghost"} or max(len(public), len(api)) > 512 or
            urlsplit(public).hostname.removeprefix("www.") != scope["domain"].removeprefix("www.") or
            platform == "wordpress" and urlsplit(public).hostname != urlsplit(api).hostname):
        raise _deny()
    return public, api


def validate(value, *, now=None):
    """Validate closed display metadata, never current owner or execution authority."""
    try:
        if (not isinstance(value, dict) or set(value) != FIELDS or type(value["version"]) is not int or value["version"] != 1 or
                value["kind"] != "cms_onboarding_offer" or type(value["channel_revision"]) is not int or not 1 <= value["channel_revision"] < 10**12 or
                type(value["first_article_points"]) is not int or not 0 <= value["first_article_points"] < 10**9 or
                type(value["setup_points"]) is not int or value["setup_points"] != 0 or value["target"] != "first_article_live" or
                value["image_placement"] != "article"):
            raise _deny()
        scope = _scope(value["scope"])
        public, api = _destinations(scope, value["platform"], value["public_origin"], value["api_origin"])
        quoted, expires = _time(value["quoted_at"]), _time(value["expires_at"])
        if (public != value["public_origin"] or api != value["api_origin"] or expires != quoted + TTL or
                now is not None and not quoted <= now < expires or len(canonical_bytes(value)) > 8192 or
                value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise _deny()
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def for_context(context, config, *, user, data, now=None):
    """Read actual initial-only choices without HTTP, key processing or a hold.

    Future acceptance must independently re-read current authority and price;
    neither a caller-supplied offer nor its digest admits any work.
    """
    if not getattr(settings, "ARTICLES_CMS_ONBOARDING_ENABLED", False) or not setup_enabled(context.organization.pk):
        return None
    if not isinstance(data, dict) or set(data) != INPUT_FIELDS:
        raise _deny()
    try:
        from .billing import get_content_factory_article_cost_points
        def current():
            for item in (context.organization, context.profile, context.company, user, config): item.refresh_from_db()
            if getattr(config, "github_repo", None) not in (None, ""):
                raise _deny()
            scope = _scope(_owner(context, config, user))
            _require_current(context, config, data["channel_scope"], data["channel_revision"])
            record = saved_record(config.article_system)
            if (record is None or record["scope"] != scope or not record["selection"] or record["selection"]["channel"] != "cms" or
                    record["selection"]["platform"] not in {"unknown", data["platform"]}):
                raise _deny()
            price = get_content_factory_article_cost_points(context.organization.domain)
            if type(price) is not int or not 0 <= price < 10**9:
                raise _deny()
            return scope, record, price
        scope, record, price = current()
        public, api = _destinations(scope, data["platform"], data["public_origin"], data["api_origin"])
        at = now or timezone.now()
        value = {"version": 1, "kind": "cms_onboarding_offer", "scope": scope, "channel_revision": record["revision"],
            "platform": data["platform"], "public_origin": public, "api_origin": api, "first_article_points": price,
            "setup_points": 0, "target": "first_article_live", "image_placement": "article", "quoted_at": at.isoformat(), "expires_at": (at + TTL).isoformat()}
        if current() != (scope, record, price):
            raise _deny()
        return validate({**value, "digest": digest(value)}, now=at)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
