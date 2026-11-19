"""E6: the server-owned setup state for every cohort, and the cohort-marker backfill (D15)."""
from io import StringIO
import itertools
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory import founder_journey as journeys
from content_factory import founder_journey_cohort as cohorts
from content_factory import founder_setup_state as setup
from content_factory.founder_journey_contract import FounderContractMismatch, _schema, _validate
from content_factory.founder_journey_testing import (
    ALL_P1_FLAGS_ON, LEGACY_CREATED_AT, _organization, _owner, make_history_free_org, make_named_legacy_org,
    make_new_journey_org,
)
from content_factory.management.commands import founder_journey_marker_backfill as backfill_command

JOURNEY_URL = "/api/v1/my-startup/journey/"


def _validate_setup(body):
    schema = _schema()
    _validate(body, schema["$defs"]["FounderSetupState"], schema["$defs"])


def _fact_combinations():
    articles = [("verified", None), ("working", None), ("fixing", None), ("not_started", None),
                *(("ask", ask) for ask in setup.SETUP_ASKS), ("ask", "not_an_ask")]
    for github, readers, authors, (page, ask) in itertools.product(
            setup.GITHUB_FACTS + ("unexpected",), (True, False), (True, False), articles):
        facts = {"github": github, "readers": readers, "authors": authors, "articlesPage": page}
        if ask:
            facts.update(articlesPageAsk=ask, askUrl="https://github.com/example/site/pull/7")
        yield facts


class SetupDecisionMatrixTests(SimpleTestCase):
    def test_every_combination_has_exactly_one_current_step_and_at_most_one_ask(self):
        count = 0
        for facts in _fact_combinations():
            count += 1
            with self.subTest(facts=facts):
                body = setup.decide_setup(facts)
                _validate_setup(body)
                states = [step["state"] for step in body["steps"]]
                self.assertEqual([step["id"] for step in body["steps"]], list(setup.STEP_IDS))
                active = [step["id"] for step in body["steps"] if step["state"] not in {"done", "todo"}]
                if body["current"] == setup.COMPLETE:
                    self.assertEqual(states, ["done"] * 4)
                    self.assertEqual((body["state"], body["ask"]), ("done", None))
                else:
                    # Exactly one current step: the first one not done, in the approved order.
                    self.assertEqual(active, [body["current"]])
                    first = next(step["id"] for step in body["steps"] if step["state"] != "done")
                    self.assertEqual(first, body["current"])
                    self.assertEqual(body["state"], states[setup.STEP_IDS.index(body["current"])])
                    after = states[setup.STEP_IDS.index(body["current"]) + 1:]
                    self.assertTrue(all(state in {"done", "todo"} for state in after))
                # At most one ask, and only on the founder's turn.
                self.assertEqual(body["ask"] is not None, body["state"] == "your_turn")
                if body["ask"]:
                    self.assertIn(body["ask"]["id"], setup.SETUP_ASKS)
                    self.assertTrue(body["ask"]["label"])
        self.assertGreater(count, 400)

    def test_the_approved_order_github_first(self):
        body = setup.decide_setup({"github": "none", "readers": False, "authors": False, "articlesPage": "not_started"})
        self.assertEqual((body["current"], body["ask"]["id"]), ("github", "connect_github"))
        body = setup.decide_setup({"github": "connected", "readers": False, "authors": True, "articlesPage": "verified"})
        self.assertEqual((body["current"], body["ask"]["id"]), ("readers", "describe_customer"))
        body = setup.decide_setup({"github": "connected", "readers": True, "authors": False, "articlesPage": "verified"})
        self.assertEqual((body["current"], body["ask"]["id"]), ("authors", "add_authors"))
        body = setup.decide_setup({"github": "connected", "readers": True, "authors": True, "articlesPage": "fixing"})
        self.assertEqual((body["current"], body["state"], body["ask"]), ("articles_page", "fixing", None))

    def test_an_operation_ask_keeps_its_https_link_only(self):
        facts = {"github": "connected", "readers": True, "authors": True, "articlesPage": "ask",
                 "articlesPageAsk": "approve_on_github", "askUrl": "http://insecure.example/pr"}
        self.assertIsNone(setup.decide_setup(facts)["ask"]["url"])
        facts["askUrl"] = "https://github.com/example/site/pull/7"
        self.assertEqual(setup.decide_setup(facts)["ask"]["url"], facts["askUrl"])

    def test_the_schema_rejects_two_current_steps_or_an_unknown_ask(self):
        body = setup.decide_setup({"github": "none", "readers": True, "authors": True, "articlesPage": "verified"})
        broken = {**body, "ask": {**body["ask"], "id": "merge_pr"}}
        with self.assertRaises(FounderContractMismatch):
            _validate_setup(broken)
        reordered = {**body, "steps": list(reversed(body["steps"]))}
        with self.assertRaises(FounderContractMismatch):
            _validate_setup(reordered)

    def test_requested_only_on_an_explicit_value(self):
        self.assertTrue(setup.requested("1"))
        for value in (None, "", "0", "no", "setup"):
            self.assertFalse(setup.requested(value))


def _approve_audience(config):
    from content_factory.editorial_catalog import approve_catalog, review_payload, update_catalog
    strategy = update_catalog(dict(config.pillar_strategy or {}), {
        "expected_editorial_catalog_version": 0,
        "audience_options": [{"id": "BUILDER", "reader_task": "Show tested work", "allow_no_offer": True}]})
    review = review_payload(strategy)
    strategy = approve_catalog(strategy, {"expected_editorial_catalog_version": review["editorial_catalog_version"],
                                          "entries": review["review_entries"]}, actor_id="1", approved_at=timezone.now())
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


def _name_authors(organization):
    from startup_updates.models import StartupProfile
    StartupProfile.objects.update_or_create(organization=organization, defaults={"founder_names": ["Ada Founder"]})


def _connect(organization, *, state="connected", repo="example/site", verified=False):
    from content_factory.website_models import WebsiteConnection, WebsiteConnectionTarget
    connection = WebsiteConnection.objects.create(organization=organization, github_repo=repo, installation_id="1",
                                                  branch="main", state=state)
    if verified:
        WebsiteConnectionTarget.objects.create(connection=connection, target_key="articles",
                                               generation=connection.generation, verified_at=timezone.now())
    return connection


GITHUB_SHAPES = ("none", "revoked", "no_repository", "connected", "verified")


def _shape(organization, github):
    if github == "revoked":
        _connect(organization, state="revoked")
    elif github == "no_repository":
        _connect(organization, repo="")
    elif github == "connected":
        _connect(organization)
    elif github == "verified":
        _connect(organization, verified=True)


@override_settings(**ALL_P1_FLAGS_ON)
class SetupStateBothCohortsTests(TestCase):
    """Every combination of stored facts, in both cohorts, returns one current step and at most one ask."""

    def build(self, cohort, github, readers, authors):
        if cohort == "legacy":
            built = make_history_free_org()
        else:
            built = make_new_journey_org()
        _shape(built.organization, github)
        if readers:
            built.config.refresh_from_db()
            _approve_audience(built.config)
        if authors:
            _name_authors(built.organization)
        built.config.refresh_from_db()
        return built

    def journey(self, built, **kwargs):
        from django.core.cache import cache
        cache.clear()
        return journeys.project_founder_journey(built.organization, user=built.owner, company=built.company, **kwargs)

    def test_every_stored_combination_in_both_cohorts(self):
        for cohort, github, readers, authors in itertools.product(("legacy", "new_journey"), GITHUB_SHAPES,
                                                                  (True, False), (True, False)):
            with self.subTest(cohort=cohort, github=github, readers=readers, authors=authors):
                built = self.build(cohort, github, readers, authors)
                body = self.journey(built, include_setup=True)
                self.assertEqual(body["cohort"], cohort)
                state = body["setup"]
                _validate_setup(state)
                active = [step["id"] for step in state["steps"] if step["state"] not in {"done", "todo"}]
                self.assertEqual(active, [] if state["current"] == setup.COMPLETE else [state["current"]])
                self.assertEqual(state["ask"] is not None, state["state"] == "your_turn")
                expected_github = {"none": "connect_github", "revoked": "authorize_github",
                                   "no_repository": "choose_repository"}.get(github)
                if expected_github:
                    self.assertEqual((state["current"], state["ask"]["id"]), ("github", expected_github))
                elif not readers:
                    self.assertEqual((state["current"], state["ask"]["id"]), ("readers", "describe_customer"))
                elif not authors:
                    self.assertEqual((state["current"], state["ask"]["id"]), ("authors", "add_authors"))
                elif github == "verified":
                    self.assertEqual((state["current"], state["ask"]), (setup.COMPLETE, None))
                else:
                    self.assertEqual((state["current"], state["ask"]["id"]), ("articles_page", "set_up_articles_page"))
                # The same facts give the same state whatever the cohort.
                self.assertEqual(state, setup.setup_state(built.organization, company=built.company, user=built.owner))

    def test_a_domainless_startup_is_asked_for_its_website(self):
        org, config = _organization(f"x-{uuid4().hex[:6]}.invalid", "No site", created_at=LEGACY_CREATED_AT)
        owner, company = _owner(org)
        body = journeys.project_founder_journey(org, user=owner, company=company, include_setup=True)
        self.assertEqual((body["setup"]["current"], body["setup"]["ask"]["id"]), ("github", "add_website"))

    def test_without_the_request_neither_cohort_changes(self):
        legacy = make_named_legacy_org("talathrive")
        body = self.journey(legacy)
        self.assertEqual(body, journeys.legacy_payload(legacy.company.pk))
        self.assertNotIn("setup", body)
        new = make_new_journey_org()
        self.assertNotIn("setup", self.journey(new))

    def test_the_legacy_setup_reads_no_article_or_entitlement_state(self):
        legacy = make_named_legacy_org("la_vie")
        with patch("content_factory.first_article_entitlement.entitlement_state") as entitlement, \
                patch("content_factory.founder_journey.latest_article_run") as article:
            body = self.journey(legacy, include_setup=True)
        entitlement.assert_not_called()
        article.assert_not_called()
        self.assertEqual(body["stage"], "legacy")
        self.assertEqual(body["setup"]["steps"][0], {"id": "github", "state": "done"})
        # D15: the legacy body is today's body plus the setup state, nothing else.
        self.assertEqual({key: value for key, value in body.items() if key not in {"setup", "etag"}},
                         {key: value for key, value in journeys.legacy_payload(legacy.company.pk).items()
                          if key != "etag"})

    def test_a_setup_pull_request_waiting_on_github_is_the_one_ask(self):
        built = self.build("legacy", "connected", True, True)
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.create(
            run_id=f"setup-{uuid4().hex[:10]}", workflow="article_system_setup", domain=built.organization.domain,
            organization=built.organization, status="blocked",
            result={"failure_class": "needs_user", "user_action": {"id": "merge_pr"},
                    "pr_url": "https://github.com/example/site/pull/9"})
        with patch("content_factory.founder_journey.registry_action_id", return_value="merge_pr"):
            state = self.journey(built, include_setup=True)["setup"]
        self.assertEqual((state["current"], state["ask"]["id"], state["ask"]["url"]),
                         ("articles_page", "approve_on_github", "https://github.com/example/site/pull/9"))
        run.status = "running"
        run.result = {}
        run.save(update_fields=["status", "result"])
        state = self.journey(built, include_setup=True)["setup"]
        self.assertEqual((state["current"], state["state"], state["ask"]), ("articles_page", "working", None))
        run.status = "failed"
        run.result = {"error_code": "SOMETHING_OURS", "failure_class": "transient"}
        run.save(update_fields=["status", "result"])
        state = self.journey(built, include_setup=True)["setup"]
        self.assertEqual((state["current"], state["state"], state["ask"]), ("articles_page", "fixing", None))


@override_settings(**ALL_P1_FLAGS_ON)
class SetupStateEndpointTests(TestCase):
    def setUp(self):
        patcher = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, built, **params):
        client = APIClient()
        client.force_authenticate(user=built.owner)
        return client.get(JOURNEY_URL, {"company_id": str(built.company.pk), **params})

    def test_setup_only_when_asked_for_in_both_cohorts(self):
        for built in (make_named_legacy_org("vyavos"), make_new_journey_org()):
            with self.subTest(cohort=cohorts.cohort(built.organization)):
                plain = self.get(built)
                asked = self.get(built, setup="1")
                self.assertEqual((plain.status_code, asked.status_code), (200, 200))
                self.assertNotIn("setup", plain.data)
                self.assertIn(asked.data["setup"]["current"], setup.STEP_IDS + (setup.COMPLETE,))
                self.assertNotEqual(plain["ETag"], asked["ETag"])
                again = APIClient()
                again.force_authenticate(user=built.owner)
                cached = again.get(JOURNEY_URL, {"company_id": str(built.company.pk), "setup": "1"},
                                   HTTP_IF_NONE_MATCH=asked["ETag"])
                self.assertEqual(cached.status_code, 304)


@override_settings(**ALL_P1_FLAGS_ON)
class MarkerBackfillTests(TestCase):
    def fixture(self):
        from content_factory.models import OrganizationContentConfig
        from organizations.models import Organization
        marked_new = make_new_journey_org()
        launch_window = make_new_journey_org(marker=False)
        legacy_unmarked = make_history_free_org()
        named = make_named_legacy_org("pawshare")
        no_config = make_history_free_org()
        OrganizationContentConfig.objects.filter(organization=no_config.organization).delete()
        islands = make_history_free_org()
        OrganizationContentConfig.objects.filter(organization=islands.organization).delete()
        no_startup = Organization.objects.create(domain=f"bare-{uuid4().hex[:6]}.example.test", name="Bare")
        return {"marked_new": marked_new, "launch_window": launch_window, "legacy_unmarked": legacy_unmarked,
                "named": named, "no_config": no_config, "islands": islands}, no_startup

    def run_backfill(self, islands_org_id, **kwargs):
        with patch.object(backfill_command, "_has_island_refresh_inputs",
                          side_effect=lambda org: org.pk == islands_org_id):
            return backfill_command.backfill(**kwargs)

    def test_dry_run_by_default_then_every_org_with_a_startup_has_a_marker(self):
        from content_factory.models import OrganizationContentConfig
        built, no_startup = self.fixture()
        islands = built["islands"].organization.pk
        before = {name: cohorts.cohort(item.organization) for name, item in built.items()}
        strategies = {row.organization_id: row.pillar_strategy for row in OrganizationContentConfig.objects.all()}
        missing = set(backfill_command.organizations_without_marker())
        self.assertEqual(missing, {built[name].organization.pk for name in
                                   ("launch_window", "legacy_unmarked", "no_config", "islands")})

        planned = self.run_backfill(islands)
        self.assertTrue(planned["dryRun"])
        self.assertEqual({row.organization_id: row.pillar_strategy for row in OrganizationContentConfig.objects.all()},
                         strategies)
        self.assertEqual(planned["counts"]["has_marker"], 2)
        self.assertEqual(planned["counts"]["blocked_island_inputs"], 1)

        written = self.run_backfill(islands, write=True)
        self.assertFalse(written["dryRun"])
        self.assertEqual(set(backfill_command.organizations_without_marker()), {islands})
        self.assertEqual(written[backfill_command.METRIC], 1)
        self.assertFalse(OrganizationContentConfig.objects.filter(organization_id=islands).exists())
        self.assertFalse(OrganizationContentConfig.objects.filter(organization=no_startup).exists())
        # No organisation moved between cohorts, and existing markers are untouched.
        self.assertEqual({name: cohorts.cohort(item.organization) for name, item in built.items()}, before)
        for name in ("marked_new", "named"):
            self.assertNotEqual(cohorts.read_marker(built[name].organization)["assignedBy"], "backfill")
        for name in ("launch_window", "legacy_unmarked", "no_config"):
            marker = cohorts.read_marker(built[name].organization)
            self.assertEqual((marker["assignedBy"], marker["cohort"]), ("backfill", before[name]))
        # Idempotent.
        self.assertEqual(self.run_backfill(islands, write=True)["rows"],
                         [{"id": islands, "domain": built["islands"].organization.domain,
                           "action": "blocked_island_inputs"}])

    def test_every_org_with_a_startup_has_a_marker_after_the_backfill(self):
        built = [make_new_journey_org(marker=False), make_history_free_org(), make_named_legacy_org("mlai", marker=False)]
        call_command("founder_journey_marker_backfill", "--write", stdout=StringIO())
        self.assertEqual(backfill_command.organizations_without_marker(), [])
        for item in built:
            self.assertTrue(cohorts._valid(cohorts.read_marker(item.organization)))

    def test_the_command_is_a_dry_run_unless_told_to_write(self):
        built = make_history_free_org()
        out = StringIO()
        call_command("founder_journey_marker_backfill", stdout=out)
        self.assertIn("dry_run", out.getvalue())
        self.assertIn("startups_without_marker: 0", out.getvalue())
        self.assertIsNone(cohorts.read_marker(built.organization))
        islands = make_history_free_org()
        from content_factory.models import OrganizationContentConfig
        OrganizationContentConfig.objects.filter(organization=islands.organization).delete()
        with patch.object(backfill_command, "_has_island_refresh_inputs", return_value=True), \
                self.assertRaises(CommandError):
            call_command("founder_journey_marker_backfill", "--write", stdout=StringIO())
