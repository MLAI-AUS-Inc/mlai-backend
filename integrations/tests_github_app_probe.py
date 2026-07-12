"""
Direct tests for the GitHub App liveness classifier and installation lister.

probe_installation_liveness is the sole gate that lets the reconciliation sweep
DELETE a GitHubInstallation row, so its status-code -> live/dead/unknown mapping
must be pinned: a future edit that maps a suspended 403 or a transient 5xx to
DEAD would make the sweep prune live installations. list_app_installation_ids is
the sweep's anti-mass-delete ownership check; it must fail closed (return None)
on any error rather than report an empty ownership set.
"""
from unittest import mock

from django.test import TestCase

from integrations.services import github_app
from integrations.services.github_app import (
    INSTALLATION_DEAD,
    INSTALLATION_LIVE,
    INSTALLATION_UNKNOWN,
    GitHubAppTokenError,
    list_app_installation_ids,
    probe_installation_liveness,
)


class _Resp:
    def __init__(self, status_code, payload=None, content=b"[]"):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class ProbeInstallationLivenessTests(TestCase):
    def _probe(self, status):
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=True), \
                mock.patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                mock.patch.object(github_app.http_requests, "post", return_value=_Resp(status)):
            return probe_installation_liveness("123")

    def test_200_is_live(self):
        self.assertEqual(self._probe(200), INSTALLATION_LIVE)

    def test_201_is_live(self):
        self.assertEqual(self._probe(201), INSTALLATION_LIVE)

    def test_404_is_dead(self):
        self.assertEqual(self._probe(404), INSTALLATION_DEAD)

    def test_410_is_dead(self):
        self.assertEqual(self._probe(410), INSTALLATION_DEAD)

    def test_403_suspended_is_unknown(self):
        # Suspension must never be treated as uninstalled — the row is recoverable.
        self.assertEqual(self._probe(403), INSTALLATION_UNKNOWN)

    def test_500_is_unknown(self):
        self.assertEqual(self._probe(500), INSTALLATION_UNKNOWN)

    def test_transport_error_is_unknown(self):
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=True), \
                mock.patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                mock.patch.object(github_app.http_requests, "post", side_effect=Exception("boom")):
            self.assertEqual(probe_installation_liveness("123"), INSTALLATION_UNKNOWN)

    def test_blank_id_is_unknown(self):
        self.assertEqual(probe_installation_liveness(""), INSTALLATION_UNKNOWN)

    def test_unconfigured_is_unknown_without_calling_github(self):
        post = mock.Mock()
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=False), \
                mock.patch.object(github_app.http_requests, "post", post):
            self.assertEqual(probe_installation_liveness("123"), INSTALLATION_UNKNOWN)
        post.assert_not_called()

    def test_jwt_error_is_unknown(self):
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=True), \
                mock.patch.object(github_app, "_github_app_jwt", side_effect=GitHubAppTokenError("no key")):
            self.assertEqual(probe_installation_liveness("123"), INSTALLATION_UNKNOWN)


class ListAppInstallationIdsTests(TestCase):
    def test_returns_string_id_set(self):
        resp = _Resp(200, payload=[{"id": 111}, {"id": 222}])
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=True), \
                mock.patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                mock.patch.object(github_app.http_requests, "get", return_value=resp):
            self.assertEqual(list_app_installation_ids(), {"111", "222"})

    def test_non_200_returns_none(self):
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=True), \
                mock.patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                mock.patch.object(github_app.http_requests, "get", return_value=_Resp(500)):
            self.assertIsNone(list_app_installation_ids())

    def test_transport_error_returns_none(self):
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=True), \
                mock.patch.object(github_app, "_github_app_jwt", return_value="jwt"), \
                mock.patch.object(github_app.http_requests, "get", side_effect=Exception("boom")):
            self.assertIsNone(list_app_installation_ids())

    def test_unconfigured_returns_none(self):
        with mock.patch.object(github_app, "github_app_credentials_configured", return_value=False):
            self.assertIsNone(list_app_installation_ids())
