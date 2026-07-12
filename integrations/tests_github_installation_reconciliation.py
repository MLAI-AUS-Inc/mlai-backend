"""
Staleness detection for the founder GitHub installation registry.

A founder who uninstalls the GitHub App leaves a GitHubInstallation row that
lists no repos yet still trips "registry exists" guards, hard-blocking the
legacy fallback and dead-ending the wizard (golden-repo baseline N1: user 1
held two dead drsamdonegan installations, 114549385 + 140961835). The
reconciliation sweep must prune installations GitHub confirms are gone, never
touch live/inconclusive ones, never mass-delete when the configured App is not
the one that minted the rows, throttle itself, and — belt-and-suspenders — the
"registry exists" guards must ignore confirmed-dead rows even before a sweep
runs (reusing the sweep's liveness_checked_at cache to stay cheap).
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import GitHubInstallation
from integrations.services import github_installations as gh
from integrations.services.github_app import (
    INSTALLATION_DEAD,
    INSTALLATION_LIVE,
    INSTALLATION_UNKNOWN,
)

User = get_user_model()

# The live installation GitHub confirms the configured App owns; used as the
# ownership anchor so the sweep's anti-mass-delete breaker permits pruning.
LIVE_ID = "140813074"


def _liveness_by_id(mapping, default=INSTALLATION_UNKNOWN):
    def _fake(installation_id):
        return mapping.get(str(installation_id), default)

    return _fake


def _make_install(user, installation_id, *, account_login="drsamdonegan", age_days=10, checked_at=None):
    inst = gh.upsert_github_installation(
        user=user, installation_id=installation_id, account_login=account_login,
    )
    # created_at is auto_now_add; push it into the past via queryset update so
    # the row clears the min-age filter. .update() bypasses auto_now_add.
    GitHubInstallation.objects.filter(pk=inst.pk).update(
        created_at=timezone.now() - timedelta(days=age_days),
        liveness_checked_at=checked_at,
    )
    inst.refresh_from_db()
    return inst


@override_settings()
class GitHubInstallationSweepTests(TestCase):
    def setUp(self):
        self.founder = User.objects.create_user(email="founder@x.co", slack_id="U_F")

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_stale_installation_is_pruned(self, _cfg):
        _make_install(self.founder, LIVE_ID)  # ownership anchor, stays
        _make_install(self.founder, "114549385")  # stale, pruned
        liveness = _liveness_by_id({LIVE_ID: INSTALLATION_LIVE, "114549385": INSTALLATION_DEAD})
        with mock.patch.object(gh, "probe_installation_liveness", liveness), \
                mock.patch.object(gh, "list_app_installation_ids", return_value={LIVE_ID}):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["pruned"], 1)
        self.assertEqual(summary["live"], 1)
        self.assertFalse(GitHubInstallation.objects.filter(installation_id="114549385").exists())
        self.assertTrue(GitHubInstallation.objects.filter(installation_id=LIVE_ID).exists())

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_live_installation_survives_and_is_stamped(self, _cfg):
        inst = _make_install(self.founder, LIVE_ID)
        self.assertIsNone(inst.liveness_checked_at)
        owns = mock.Mock()
        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_LIVE), \
                mock.patch.object(gh, "list_app_installation_ids", owns):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["live"], 1)
        self.assertEqual(summary["pruned"], 0)
        owns.assert_not_called()  # ownership check only runs when there are dead rows
        inst.refresh_from_db()
        self.assertIsNotNone(inst.liveness_checked_at)

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_unknown_installation_is_kept_and_stamped(self, _cfg):
        # A suspended installation / transient 5xx / network blip is inconclusive
        # and must never be deleted.
        inst = _make_install(self.founder, "555")
        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_UNKNOWN):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["pruned"], 0)
        self.assertTrue(GitHubInstallation.objects.filter(installation_id="555").exists())
        inst.refresh_from_db()
        self.assertIsNotNone(inst.liveness_checked_at)

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_fresh_installation_is_not_probed(self, _cfg):
        # created today (< min age) — protects a just-authorized install from a
        # propagation-race false-dead.
        _make_install(self.founder, "999", age_days=0)
        probe = mock.Mock(return_value=INSTALLATION_DEAD)
        with mock.patch.object(gh, "probe_installation_liveness", probe):
            summary = gh.run_github_installation_reconciliation_sweep()
        probe.assert_not_called()
        self.assertEqual(summary["checked"], 0)
        self.assertTrue(GitHubInstallation.objects.filter(installation_id="999").exists())

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_recently_checked_installation_is_throttled(self, _cfg):
        # liveness_checked_at within the probe interval -> skipped this tick.
        _make_install(self.founder, "888", checked_at=timezone.now())
        probe = mock.Mock(return_value=INSTALLATION_DEAD)
        with mock.patch.object(gh, "probe_installation_liveness", probe):
            summary = gh.run_github_installation_reconciliation_sweep()
        probe.assert_not_called()
        self.assertEqual(summary["checked"], 0)

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_batch_limit_bounds_probes_per_tick(self, _cfg):
        for i in range(4):
            _make_install(self.founder, f"batch-{i}")
        probe = mock.Mock(return_value=INSTALLATION_LIVE)
        with override_settings(GITHUB_INSTALLATION_RECONCILIATION_BATCH_LIMIT=2):
            with mock.patch.object(gh, "probe_installation_liveness", probe):
                summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(summary["checked"], 2)

    def test_sweep_skipped_when_app_unconfigured(self):
        _make_install(self.founder, "114549385")
        probe = mock.Mock(return_value=INSTALLATION_DEAD)
        with mock.patch.object(gh, "github_app_credentials_configured", return_value=False):
            with mock.patch.object(gh, "probe_installation_liveness", probe):
                summary = gh.run_github_installation_reconciliation_sweep()
        probe.assert_not_called()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["reason"], "github_app_unconfigured")
        # Nothing pruned, nothing stamped — we could not actually check.
        self.assertTrue(GitHubInstallation.objects.filter(installation_id="114549385").exists())

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_concurrent_reinstall_is_not_clobbered(self, _cfg):
        # If the row is written (auto_now updated_at bumped) between candidate
        # selection and the delete, the conditional delete must lose the race.
        _make_install(self.founder, LIVE_ID)  # ownership anchor
        _make_install(self.founder, "raced")

        def _probe(installation_id):
            if str(installation_id) == "raced":
                GitHubInstallation.objects.filter(installation_id="raced").update(
                    updated_at=timezone.now() + timedelta(seconds=5)
                )
                return INSTALLATION_DEAD
            return INSTALLATION_LIVE

        with mock.patch.object(gh, "probe_installation_liveness", _probe), \
                mock.patch.object(gh, "list_app_installation_ids", return_value={LIVE_ID}):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["pruned"], 0)
        self.assertEqual(summary["skipped_raced"], 1)
        self.assertTrue(GitHubInstallation.objects.filter(installation_id="raced").exists())

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_known_baseline_stale_rows_are_pruned_live_one_survives(self, _cfg):
        # Reproduces golden-repo baseline N1: user's two dead drsamdonegan rows
        # disappear; the live one is untouched.
        _make_install(self.founder, "114549385")
        _make_install(self.founder, "140961835")
        _make_install(self.founder, LIVE_ID)
        liveness = _liveness_by_id({
            "114549385": INSTALLATION_DEAD,
            "140961835": INSTALLATION_DEAD,
            LIVE_ID: INSTALLATION_LIVE,
        })
        with mock.patch.object(gh, "probe_installation_liveness", liveness), \
                mock.patch.object(gh, "list_app_installation_ids", return_value={LIVE_ID}):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["pruned"], 2)
        self.assertEqual(summary["live"], 1)
        remaining = set(
            GitHubInstallation.objects.filter(user=self.founder).values_list(
                "installation_id", flat=True
            )
        )
        self.assertEqual(remaining, {LIVE_ID})

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_all_dead_pass_withheld_when_app_owns_no_local_rows(self, _cfg):
        # Every probe 404s and the configured App owns none of this DB's rows ->
        # credential/App-id mismatch, not real uninstalls -> refuse to delete.
        _make_install(self.founder, "114549385")
        _make_install(self.founder, "140961835")
        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_DEAD), \
                mock.patch.object(gh, "list_app_installation_ids", return_value={"some-other-apps-install"}):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["status"], "aborted_ownership_unconfirmed")
        self.assertEqual(summary["dead_withheld"], 2)
        self.assertEqual(summary["pruned"], 0)
        self.assertEqual(GitHubInstallation.objects.filter(user=self.founder).count(), 2)

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_dead_rows_withheld_when_installation_list_unavailable(self, _cfg):
        # list_app_installation_ids returns None (transient/unreadable) -> cannot
        # confirm ownership -> do not delete.
        _make_install(self.founder, "114549385")
        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_DEAD), \
                mock.patch.object(gh, "list_app_installation_ids", return_value=None):
            summary = gh.run_github_installation_reconciliation_sweep()
        self.assertEqual(summary["status"], "aborted_ownership_unconfirmed")
        self.assertEqual(summary["pruned"], 0)
        self.assertTrue(GitHubInstallation.objects.filter(installation_id="114549385").exists())


@override_settings()
class GitHubRegistryGuardResilienceTests(TestCase):
    """user_has_registered_installation is the read-time guard that keeps a
    stale-only registry from blocking the legacy fallback even before a sweep
    prunes it — while reusing the sweep's liveness_checked_at cache to avoid
    re-probing GitHub on every request."""

    def setUp(self):
        self.founder = User.objects.create_user(email="founder@x.co", slack_id="U_F")

    def test_empty_registry_is_not_registered(self):
        self.assertFalse(gh.user_has_registered_installation(self.founder))

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_all_dead_registry_is_not_registered(self, _cfg):
        _make_install(self.founder, "114549385")
        _make_install(self.founder, "140961835")
        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_DEAD):
            self.assertFalse(gh.user_has_registered_installation(self.founder))

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_any_live_installation_is_registered(self, _cfg):
        _make_install(self.founder, "114549385")
        _make_install(self.founder, LIVE_ID)
        liveness = _liveness_by_id({"114549385": INSTALLATION_DEAD, LIVE_ID: INSTALLATION_LIVE})
        with mock.patch.object(gh, "probe_installation_liveness", liveness):
            self.assertTrue(gh.user_has_registered_installation(self.founder))

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_unknown_installation_still_counts_as_registered(self, _cfg):
        # Conservative: an inconclusive probe must NOT un-block the legacy path.
        _make_install(self.founder, "555")
        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_UNKNOWN):
            self.assertTrue(gh.user_has_registered_installation(self.founder))

    def test_unconfigured_app_counts_registry_unchanged_without_probing(self):
        _make_install(self.founder, "555")
        probe = mock.Mock(return_value=INSTALLATION_DEAD)
        with mock.patch.object(gh, "github_app_credentials_configured", return_value=False):
            with mock.patch.object(gh, "probe_installation_liveness", probe):
                self.assertTrue(gh.user_has_registered_installation(self.founder))
        probe.assert_not_called()

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_recent_liveness_stamp_short_circuits_without_probing(self, _cfg):
        # A row the sweep stamped within the probe interval is trusted as non-dead
        # with NO network call — the fix for the per-request GitHub-probe hazard.
        _make_install(self.founder, LIVE_ID, checked_at=timezone.now())
        probe = mock.Mock(return_value=INSTALLATION_DEAD)
        with mock.patch.object(gh, "probe_installation_liveness", probe):
            self.assertTrue(gh.user_has_registered_installation(self.founder))
        probe.assert_not_called()

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_stale_liveness_stamp_is_reprobed(self, _cfg):
        # A stamp older than the probe interval no longer certifies liveness.
        _make_install(self.founder, "114549385", checked_at=timezone.now() - timedelta(hours=48))
        probe = mock.Mock(return_value=INSTALLATION_DEAD)
        with mock.patch.object(gh, "probe_installation_liveness", probe):
            self.assertFalse(gh.user_has_registered_installation(self.founder))
        probe.assert_called_once()


@override_settings()
class ResolveInstallationFallbackTests(TestCase):
    """The read-time guard must let a stale-only registry fall through to the
    legacy per-org installation id instead of returning a dead id or ""."""

    def setUp(self):
        from content_factory.models import OrganizationContentConfig
        from organizations.models import Organization

        self.founder = User.objects.create_user(email="founder@x.co", slack_id="U_F")
        self.org = Organization.objects.create(name="Cowork", domain="coworkadelaide.com.au")
        self.cfg = OrganizationContentConfig.objects.create(
            organization=self.org,
            connected_slack_user_id=f"mlai_user:{self.founder.id}",
            github_repo="drsamdonegan/golden-next-baseline",
            github_installation_id=LIVE_ID,  # live per-org id, recoverable
        )

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_single_stale_owner_match_falls_back_to_per_org_id(self, _cfg):
        # The real installation_for_repo matches the sole owner (dead) row with no
        # liveness check; the resolver must reject the dead id and use the per-org
        # id instead of dead-ending. (No installation_for_repo mock — the point is
        # to exercise the real owner-match short-circuit.)
        _make_install(self.founder, "114549385")  # single stale, owner drsamdonegan
        from content_factory.vibe_marketing_views import _resolve_installation_id_for_repo

        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_DEAD):
            resolved = _resolve_installation_id_for_repo(
                self.cfg, "drsamdonegan/golden-next-baseline"
            )
        self.assertEqual(resolved, LIVE_ID)

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    @mock.patch.object(gh, "installation_for_repo", return_value=None)
    def test_live_registry_mismatch_still_returns_empty(self, _m_ifr, _cfg):
        # A live installation that cannot resolve the repo is a genuine ownership
        # mismatch — the deliberate "don't recreate the stale tuple" behavior is
        # preserved.
        _make_install(self.founder, "144909617", account_login="other-owner")
        from content_factory.vibe_marketing_views import _resolve_installation_id_for_repo

        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_LIVE):
            resolved = _resolve_installation_id_for_repo(
                self.cfg, "drsamdonegan/golden-next-baseline"
            )
        self.assertEqual(resolved, "")


@override_settings()
class ConnectLegacyPromotionGuardTests(TestCase):
    """The legacy-promotion guard in _connect_with_existing_github_credentials
    must let a stale-only (all-dead) registry fall through to promote the legacy
    UserIntegration, and still block promotion when a live installation exists."""

    def setUp(self):
        from content_factory.models import OrganizationContentConfig
        from integrations.models import UserIntegration
        from organizations.models import Organization

        self.founder = User.objects.create_user(email="founder@x.co", slack_id="U_F")
        self.actor = f"mlai_user:{self.founder.id}"
        self.org = Organization.objects.create(name="Cowork", domain="coworkadelaide.com.au")
        self.cfg = OrganizationContentConfig.objects.create(
            organization=self.org, connected_slack_user_id=self.actor,
        )
        UserIntegration.objects.create(
            slack_user_id=self.actor,
            github_access_token="ghu_legacy",
            github_repo="drsamdonegan/golden-next-baseline",
            github_user_name="drsamdonegan",
        )
        _make_install(self.founder, "114549385")  # stale-only registry

    @mock.patch("content_factory.vibe_marketing_views.ensure_valid_token", return_value=None)
    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_all_dead_registry_allows_legacy_promotion(self, _cfg, _tok):
        from content_factory.vibe_marketing_views import _connect_with_existing_github_credentials

        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_DEAD):
            resp = _connect_with_existing_github_credentials(
                self.cfg,
                domain="coworkadelaide.com.au",
                actor_id=self.actor,
                requested_repo="drsamdonegan/golden-next-baseline",
            )
        self.assertIsNotNone(resp)
        self.assertEqual(resp["credential_source"], "user_promoted")

    @mock.patch("content_factory.vibe_marketing_views.ensure_valid_token", return_value=None)
    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    def test_live_registry_blocks_legacy_promotion(self, _cfg, _tok):
        from content_factory.vibe_marketing_views import _connect_with_existing_github_credentials

        with mock.patch.object(gh, "probe_installation_liveness", return_value=INSTALLATION_LIVE):
            resp = _connect_with_existing_github_credentials(
                self.cfg,
                domain="coworkadelaide.com.au",
                actor_id=self.actor,
                requested_repo="drsamdonegan/golden-next-baseline",
            )
        self.assertIsNone(resp)  # live registry is authoritative -> no legacy promotion
