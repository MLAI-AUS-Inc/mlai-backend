"""Test-only helpers for the P1 founder journey. Never import this from production code.

- :data:`ALL_P1_FLAGS_ON`: an ``override_settings`` dict with every P1 backend
  switch on and LAUNCHED_AT one day in the past.
- :func:`make_named_legacy_org`: the five named legacy organisations (La Vie,
  TalaThrive, Vyavos, PawShare, mlai.au) in their production shapes, with or
  without a ``report_apply`` legacy marker. Both variants are created before
  LAUNCHED_AT.
- :func:`make_history_free_org` and :func:`make_new_journey_org`: the other
  two cohorts the report and the gates distinguish.

Every backend P1 package proves its legacy guarantee with these builders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional
from uuid import uuid4

from django.utils import timezone

from . import founder_journey_cohort as cohort_module

P1_FLAG_NAMES = (
    "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED",
    "MY_STARTUP_FOUNDER_JOURNEY_API_ENABLED",
    "MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED",
    "COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED",
    "COMMUNITY_CHAT_INVITE_TOKENS_ENABLED",
    "MY_STARTUP_PENDING_MEMBER_ACCESS_ENABLED",
    "COMMUNITY_CHAT_REVIEW_CLEARED_EMAIL_ENABLED",
    "COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED",
    "MY_STARTUP_ONE_FIELD_START_ENABLED",
    "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED",
    "ARTICLES_FIRST_ARTICLE_CARD_ENABLED",
    "ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED",
    "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED",
    "ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED",
    "ARTICLES_DRAFT_TOOLS_ENABLED",
    "ARTICLES_LIVE_URL_VERIFICATION_ENABLED",
    "MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED",
)
# Documented code defaults of every P1 backend setting (contract section 5).
P1_SETTING_DEFAULTS = {
    **{name: False for name in P1_FLAG_NAMES},
    "MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT": "",
    "MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS": "",
    "COMMUNITY_CHAT_INVITE_TOKEN_DEFAULT_TTL_HOURS": 72,
    "COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS": 336,
    "COMMUNITY_CHAT_INVITE_RESOLVE_RATE": "30/minute",
    "COMMUNITY_CHAT_DEVICE_AUTH_MAX_LIFETIME_SECONDS": 3600,
}

LAUNCHED_AT = datetime.now(dt_timezone.utc).replace(microsecond=0) - timedelta(days=1)
ALL_P1_FLAGS_ON = {
    **{name: True for name in P1_FLAG_NAMES},
    "MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT": LAUNCHED_AT.isoformat(),
    "MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS": "",
}
# A pre-launch creation time for every legacy builder.
LEGACY_CREATED_AT = datetime(2026, 3, 2, 9, 30, tzinfo=dt_timezone.utc)

NAMED_LEGACY_ORGS = {
    "talathrive": "talathrive.com",
    "vyavos": "vyavos.com",
    "pawshare": "pawshareclub.com",
    "la_vie": "la-vie.com.au",
    "mlai": "mlai.au",
}


@dataclass
class FounderJourneyOrg:
    """What a builder made: the organisation, its config and its owner's company."""
    organization: object
    config: object
    owner: object
    company: object
    connection: Optional[object] = None
    target: Optional[object] = None
    hosted_site: Optional[object] = None
    runs: list = field(default_factory=list)
    articles: list = field(default_factory=list)


def _owner(org, *, email=None):
    from core.models import User
    from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
    user = User.objects.create_user(email=email or f"owner-{uuid4().hex[:10]}@synthetic.example.test")
    profile = VibeRaisingProfile.objects.create(user=user, role=VibeRaisingProfile.ROLE_FOUNDER)
    company = VibeRaisingCompany.objects.create(profile=profile, organization=org, name=org.name, domain=org.domain)
    profile.active_company = company
    profile.save(update_fields=["active_company", "updated_at"])
    return user, company


def _set_created_at(model_object, when):
    type(model_object).objects.filter(pk=model_object.pk).update(created_at=when)
    model_object.refresh_from_db()


def _organization(domain, name, *, created_at):
    from organizations.models import Organization
    from .models import OrganizationContentConfig
    org = Organization.objects.create(domain=domain, name=name)
    _set_created_at(org, created_at)
    config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)
    return org, config


def _connect(org, config, *, repo, adapter="next_app_router", contract=None, site_url=None):
    from .website_models import WebsiteConnection, WebsiteConnectionTarget
    verified = timezone.now() - timedelta(days=3)
    connection = WebsiteConnection.objects.create(
        organization=org, github_repo=repo, installation_id="1000001", branch="main",
        site_url=site_url or f"https://{org.domain}", state=WebsiteConnection.State.CONNECTED,
        verified_sha="a" * 40, last_verified_at=verified)
    target = WebsiteConnectionTarget.objects.create(
        connection=connection, target_key="articles", generation=connection.generation, adapter=adapter,
        source_sha="a" * 40, contract=contract or {"route": "/blog/{slug}"}, verified_at=verified)
    config.website_connection = connection
    config.github_repo = repo
    config.article_delivery_mode = "review_draft"
    config.save(update_fields=["website_connection", "github_repo", "article_delivery_mode", "updated_at"])
    return connection, target


def _review_draft_run(org, *, status="completed"):
    from workflow_runs.models import ContentFactoryRun
    return ContentFactoryRun.objects.create(
        run_id=f"run-{uuid4().hex[:12]}", workflow="article_generation", domain=org.domain, organization=org,
        status=status, current_step="deliver", run_request={"delivery_mode": "review_draft", "topic": "A delivered article"},
        result={"delivery_mode": "review_draft", "pr_url": f"https://github.com/example/{org.pk}/pull/1"})


def _article(org, *, slug, published=True, run=None):
    from .models import ArticlePublishStatus, WrittenArticle
    now = timezone.now()
    return WrittenArticle.objects.create(
        organization=org, title=f"Article {slug}", slug=slug, category="guides", primary_keyword=slug.replace("-", " "),
        publish_status=ArticlePublishStatus.LIVE if published else ArticlePublishStatus.PR_OPEN,
        pr_url=f"https://github.com/example/{org.pk}/pull/2",
        live_url=f"https://{org.domain}/blog/{slug}" if published else None,
        published_at=now - timedelta(days=2) if published else None,
        live_verified_at=now - timedelta(days=2) if published else None,
        source_run_id=getattr(run, "run_id", ""))


def _write_legacy_marker(org, config):
    config.refresh_from_db()
    reasons = cohort_module.legacy_reasons(org, config)
    inventory = cohort_module.founder_state_inventory(org, config)
    marker = cohort_module._base_marker(cohort_module.LEGACY, "report_apply",
                                        [*reasons, *(f"inventory:{signal}" for signal in inventory)])
    cohort_module._save_marker(config, marker)
    config.refresh_from_db()


def make_named_legacy_org(name, *, marker=True, domain=None, owner_email=None) -> FounderJourneyOrg:
    """Build one of the five named legacy organisations in its production shape.

    ``name`` is ``"talathrive"``, ``"vyavos"``, ``"pawshare"``, ``"la_vie"``
    or ``"mlai"``. With ``marker=True`` the organisation also carries the
    ``report_apply`` legacy marker that ``--apply`` writes; with
    ``marker=False`` it has no marker. Both are created before LAUNCHED_AT.

    - TalaThrive and Vyavos: verified GitHub publishers (connected
      ``WebsiteConnection`` with a verified target) with delivered
      review_draft articles.
    - PawShare: an active ``HostedArticleSite`` alongside its repository
      connection.
    - La Vie: an adopted-registry connection with a verified target.
    - mlai.au: the free Content Factory domain with published articles.
    """
    from .hosted_article_models import HostedArticleSite
    if name not in NAMED_LEGACY_ORGS:
        raise ValueError(f"Unknown named legacy org: {name}")
    domain = domain or NAMED_LEGACY_ORGS[name]
    org, config = _organization(domain, name.replace("_", " ").title(), created_at=LEGACY_CREATED_AT)
    owner, company = _owner(org, email=owner_email)
    built = FounderJourneyOrg(organization=org, config=config, owner=owner, company=company)
    if name in {"talathrive", "vyavos"}:
        built.connection, built.target = _connect(org, config, repo=f"example-{name}/website")
        for index in range(2):
            run = _review_draft_run(org)
            built.runs.append(run)
            built.articles.append(_article(org, slug=f"{name}-article-{index}", published=index == 0, run=run))
    elif name == "pawshare":
        built.connection, built.target = _connect(org, config, repo="example-pawshare/website")
        built.hosted_site = HostedArticleSite.objects.create(
            organization=org, domain=org.domain, base_path="/blog/", state=HostedArticleSite.State.ACTIVE,
            worker_name="pawshare-articles", proxy_verified_at=timezone.now() - timedelta(days=4),
            last_deployed_at=timezone.now() - timedelta(days=1))
        run = _review_draft_run(org)
        built.runs.append(run)
        built.articles.append(_article(org, slug="pawshare-article-0", run=run))
    elif name == "la_vie":
        built.connection, built.target = _connect(
            org, config, repo="example-la-vie/website", adapter="adopted_registry",
            contract={"mode": "adopted_registry", "registry_path": "src/data/articles.ts", "route": "/journal/{slug}"})
        run = _review_draft_run(org)
        built.runs.append(run)
        built.articles.append(_article(org, slug="la-vie-article-0", published=False, run=run))
    else:  # mlai.au
        built.connection, built.target = _connect(org, config, repo="example-mlai/website")
        for index in range(2):
            built.articles.append(_article(org, slug=f"mlai-article-{index}"))
    if marker:
        _write_legacy_marker(org, config)
    config.refresh_from_db()
    built.config = config
    return built


def make_history_free_org(domain=None, *, owner=True, created_at=LEGACY_CREATED_AT) -> FounderJourneyOrg:
    """An organisation with no history and no state, created before LAUNCHED_AT, without a marker."""
    org, config = _organization(domain or f"fresh-{uuid4().hex[:8]}.example.test", "History free", created_at=created_at)
    user, company = _owner(org) if owner else (None, None)
    return FounderJourneyOrg(organization=org, config=config, owner=user, company=company)


def make_new_journey_org(domain=None, *, marker=True, create_key_sha256=None) -> FounderJourneyOrg:
    """A startup created after :data:`LAUNCHED_AT`, with a ``create`` marker (or none, for launch-window only)."""
    org, config = _organization(domain or f"new-{uuid4().hex[:8]}.example.test", "New journey",
                                created_at=timezone.now())
    user, company = _owner(org)
    if marker:
        fresh = cohort_module._base_marker(cohort_module.NEW_JOURNEY, "create", ["created_after_launch"])
        fresh.update(createKeySha256=create_key_sha256, createdCompanyId=str(company.pk))
        cohort_module._save_marker(config, fresh)
        config.refresh_from_db()
    return FounderJourneyOrg(organization=org, config=config, owner=user, company=company)
