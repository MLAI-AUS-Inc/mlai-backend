"""E6: the server-owned setup state, the same for every cohort (decision D15).

The founder journey answers with ``setup`` when the client asks for it
(``?setup=1``): the four guided steps in the approved order (GitHub, target
readers, authors, articles page), the one current step and at most one ask.
Target readers and Authors are optional: they show done when saved and todo
otherwise, and are never the current step, so only GitHub and the articles
page can hold setup or the first article.
It is computed here from the facts the clients read for themselves today
(Chat ``setupProgress.ts`` and mobile ``startup_setup_progress.dart``), so the
clients stop deriving the step and only render it.

Every state is one of three: the founder's turn (one named ask), working on it
(nothing for the founder to do), or we're fixing it (ours to fix, staff are
told by the existing alert paths). Legacy organisations get the same state:
there is no second journey.

This is a read projection. Nothing here writes, spends, starts a run or
widens any authority; every command it points to rechecks its own guards.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SETUP_VERSION = 1
STEP_IDS = ("github", "readers", "authors", "articles_page")
# Owner decision 2026-11-28: Target readers and Authors are optional. They never
# carry an ask or become current; an audience the founder has not approved is
# asked once at article start instead (editorial_default_brief.ASK_ACTION).
# Their asks stay in SETUP_ASK_LABELS: the schema and older clients name them.
OPTIONAL_STEP_IDS = frozenset({"readers", "authors"})
COMPLETE = "complete"
# Step states. ``todo`` is a later step that is not current yet.
STEP_STATES = ("done", "your_turn", "working", "fixing", "todo")
CURRENT_STATES = ("your_turn", "working", "fixing", "done")

# The closed list of setup asks and the button each one carries. A client that
# cannot render an ask natively shows the generic continue card (plan E14).
SETUP_ASK_LABELS = {
    "add_website": "Add your website",
    "connect_github": "Connect GitHub",
    "authorize_github": "Give access on GitHub",
    "choose_repository": "Choose your repository",
    "describe_customer": "Describe your customer",
    "add_authors": "Add your authors",
    "set_up_articles_page": "Set up your articles page",
    "approve_setup": "Take a look",
    "approve_on_github": "Open on GitHub",
    "choose_articles_page": "Choose a page",
    "deploy_site": "How to fix this",
}
SETUP_ASKS = tuple(SETUP_ASK_LABELS)

# Facts for the GitHub step, in the order they are checked.
GITHUB_FACTS = ("no_website", "none", "disconnected", "revoked", "no_repository", "connected")
# Facts for the articles-page step. ``ask`` carries an ask id beside it.
ARTICLES_PAGE_FACTS = ("verified", "ask", "working", "fixing", "not_started")

_SETUP_ACTIVE = frozenset({"queued", "running", "processing", "pending", "starting", "in_progress",
                           "awaiting_approval", "approval_required", "awaiting_confirmation",
                           "awaiting_delivery_mode", "preview_building", "preview_verifying",
                           "repair_preview_building", "completed"})


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _github_step(fact):
    if fact == "connected":
        return True, "done", None
    ask = {"no_website": "add_website", "none": "connect_github", "disconnected": "connect_github",
           "revoked": "authorize_github", "no_repository": "choose_repository"}.get(fact, "connect_github")
    return False, "your_turn", ask


def _articles_page_step(fact, ask):
    if fact == "verified":
        return True, "done", None
    if fact == "ask" and ask in SETUP_ASK_LABELS:
        return False, "your_turn", ask
    if fact == "working":
        return False, "working", None
    if fact == "fixing":
        return False, "fixing", None
    return False, "your_turn", "set_up_articles_page"


def decide_setup(facts: dict) -> dict:
    """The setup body for a dict of facts. Pure: no reads, no clock.

    ``facts`` keys: ``github`` (one of :data:`GITHUB_FACTS`), ``readers`` and
    ``authors`` (booleans), ``articlesPage`` (one of
    :data:`ARTICLES_PAGE_FACTS`), and optionally ``articlesPageAsk``,
    ``askUrl`` and ``askLabel`` for an operation's ask.

    The current step is the first required step (GitHub, articles page), in
    the approved order, that is not done; once both are done it is
    ``complete``. Readers and authors are optional: done when saved, else
    todo, never current and never an ask. Only the current step can carry an
    ask, so there is exactly one current step and at most one ask.
    """
    facts = _mapping(facts)
    decided = {
        "github": _github_step(facts.get("github")),
        "readers": (True, "done", None) if facts.get("readers") is True else (False, "todo", None),
        "authors": (True, "done", None) if facts.get("authors") is True else (False, "todo", None),
        "articles_page": _articles_page_step(facts.get("articlesPage"), facts.get("articlesPageAsk")),
    }
    current = next((step for step in STEP_IDS if step not in OPTIONAL_STEP_IDS and not decided[step][0]), COMPLETE)
    steps = []
    for step in STEP_IDS:
        done, state, _ask = decided[step]
        steps.append({"id": step, "state": "done" if done else state if step == current else "todo"})
    ask = None
    state = "done"
    if current != COMPLETE:
        _done, state, ask_id = decided[current]
        if state == "your_turn" and ask_id:
            url = facts.get("askUrl") if current == "articles_page" else None
            label = facts.get("askLabel") if current == "articles_page" and facts.get("askLabel") else None
            ask = {"id": ask_id, "label": label or SETUP_ASK_LABELS[ask_id],
                   "url": url if isinstance(url, str) and url.startswith("https://") else None}
    return {"version": SETUP_VERSION, "current": current, "state": state, "steps": steps, "ask": ask}


# ---------------------------------------------------------------------------
# Facts


def _github_fact(organization, connection):
    from . import founder_journey_cohort as cohorts
    from .website_models import WebsiteConnection
    if not cohorts.organization_has_website(organization):
        return "no_website"
    if connection is None:
        return "none"
    if connection.state == WebsiteConnection.State.REVOKED:
        return "revoked"
    if connection.state == WebsiteConnection.State.DISCONNECTED:
        return "disconnected"
    if not str(getattr(connection, "github_repo", "") or "").strip():
        return "no_repository"
    # Connected or paused, with a repository. Re-checking a pushed source is
    # MLAI's own work on the articles-page step, never a reason to send the
    # founder back to GitHub (the same rule as Chat setupProgress.ts).
    return "connected"


def readers_approved(config) -> bool:
    """At least one approved audience in the editorial catalogue (the client's Target readers check)."""
    from .content_first_article import _catalog
    if config is None or not getattr(config, "pk", None):
        return False
    options = (_catalog(config) or {}).get("audience_options") or []
    return any(isinstance(item, dict) and item.get("status") == "approved" for item in options)


def authors_saved(organization, config=None) -> bool:
    """At least one named founder profile or founder name (the client's Authors check)."""
    from founder_tools.profile_fields import PROFILE_KEY
    strategy = _mapping(getattr(config, "pillar_strategy", None))
    rows = _mapping(strategy.get(PROFILE_KEY)).get("founderProfiles")
    if isinstance(rows, list) and any(isinstance(row, dict) and str(row.get("name") or "").strip() for row in rows):
        return True
    from startup_updates.models import StartupProfile
    names = (StartupProfile.objects.filter(organization_id=organization.pk)
             .values_list("founder_names", flat=True).first())
    return isinstance(names, list) and any(str(name or "").strip() for name in names)


def _articles_page_facts(organization, *, company, config, user, marker, connection, setup_run):
    from . import founder_journey as journey
    from . import founder_journey_cohort as cohorts
    if cohorts.has_connected_website(organization):
        return {"articlesPage": "verified"}
    decision = None
    if connection is not None or setup_run is not None:
        try:
            decision = journey._setup_decision(organization=organization, company=company, config=config, user=user,
                                               marker=marker or {}, connection=connection, setup_run=setup_run,
                                               run=None)
        except Exception:  # noqa: BLE001 - a fact read never breaks the journey poll
            logger.warning("founder_setup_state_setup_decision_failed org=%s", organization.pk, exc_info=True)
            decision = None
    action = _mapping(_mapping(decision).get("action"))
    if action.get("id") in SETUP_ASK_LABELS:
        return {"articlesPage": "ask", "articlesPageAsk": action["id"], "askUrl": action.get("url"),
                "askLabel": action.get("label") if action["id"] == journey.DEPLOY_ACTION else None}
    if setup_run is None:
        return {"articlesPage": "not_started"}
    if setup_run.status in _SETUP_ACTIVE:
        return {"articlesPage": "working"}
    if ((setup_run.status == "cancelled" and journey.is_owner_initiated_stop(setup_run))
            or journey.is_stale_setup_run(setup_run, connection, config)):
        # The owner's cancel, Clear saved setup, a disconnect or Stop, or a stop
        # a later reset left behind: not ours to fix, so setup starts again.
        return {"articlesPage": "not_started"}
    # A stopped setup with no founder ask is ours to fix.
    return {"articlesPage": "fixing"}


def setup_facts(organization, *, company=None, config=None, user=None, marker=None, connection=None,
                setup_run=None) -> dict:
    """Read the facts :func:`decide_setup` needs. ``connection`` and ``setup_run`` are the journey's own reads."""
    return {
        "github": _github_fact(organization, connection),
        "readers": readers_approved(config),
        "authors": authors_saved(organization, config),
        **_articles_page_facts(organization, company=company, config=config, user=user, marker=marker,
                               connection=connection, setup_run=setup_run),
    }


def setup_state(organization, *, company=None, config=None, user=None, marker=None, connection=None,
                setup_run=None, read_runs=True) -> dict:
    """The ``setup`` body for ``organization`` (any cohort).

    With ``read_runs`` the latest connection and setup run are read here;
    the journey passes the ones it already read.
    """
    from . import founder_journey as journey
    from . import founder_journey_cohort as cohorts
    if config is None:
        config = cohorts._config_for(organization)
    if read_runs:
        connection = journey._connection(organization)
        setup_run = journey._setup_run(organization)
    if marker is None:
        marker = cohorts.read_marker(organization, config) if config is not None else {}
    return decide_setup(setup_facts(organization, company=company, config=config, user=user, marker=marker,
                                    connection=connection, setup_run=setup_run))


def requested(value) -> bool:
    """Whether a client asked for ``setup`` (``?setup=1``): older clients never send it."""
    return str(value or "").strip().lower() in {"1", "true", "yes"}
