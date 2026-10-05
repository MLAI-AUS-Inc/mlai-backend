"""Exercise real startup throttle decisions and HTTP errors without a database."""
from types import SimpleNamespace as Obj
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import URLResolver, resolve
from corsheaders.middleware import CorsMiddleware
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from community_chat import my_startup_urls, my_startup_views
from community_chat.startups import views as startup_views
from community_chat.throttles import CommunityChatScopedThrottle, StartupScopedThrottle
from founder_tools.my_startup import urls as alias_urls
from founder_tools.my_startup.api import MyStartupViewMixin, startup_view
from founder_tools.my_startup.roo_link import CaptureRooLinkView
from core.middleware import DesktopAuthCorsMiddleware


@override_settings(CACHES={
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "watt_session": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "watt-session-test"},
})
class StartupThrottleTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = 1000.0
        for target, name, value in (
            (StartupScopedThrottle, "timer", lambda _: self.now),
            (StartupScopedThrottle, "THROTTLE_RATES", {
                "my_startup_bootstrap": "2/minute", "my_startup_read": "2/minute",
                "my_startup_poll": "2/minute", "my_startup_write": "2/minute",
            }),
            (CommunityChatScopedThrottle, "THROTTLE_RATES", {"community_chat_home": "1/minute"}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method="GET", user=1, company="first", device="first"):
        return Obj(method=method, user=Obj(pk=user, is_authenticated=True),
                   query_params={"company_id": company, "startup_throttle_scope": "other"},
                   data={"startup_throttle_scope": "other"}, community_chat_public_key=device)

    def allowed(self, cls=my_startup_views.AccountView, **kwargs):
        view = cls()
        view.request = self.request(**kwargs)
        # APIView evaluates every throttle, including when another one denies.
        decisions = [item.allow_request(view.request, view) for item in view.get_throttles()]
        return all(decisions)

    def test_home_reads_polling_and_writes_cannot_starve_bootstrap(self):
        request = self.request()
        home = Obj(community_chat_throttle_scope="community_chat_home")
        self.assertTrue(CommunityChatScopedThrottle().allow_request(request, home))
        self.assertFalse(CommunityChatScopedThrottle().allow_request(request, home))
        for cls, method in ((my_startup_views.RepositoriesView, "GET"),
                            (my_startup_views.RunView, "GET"),
                            (my_startup_views.SettingsView, "PUT")):
            self.assertTrue(self.allowed(cls, method=method))
            self.assertTrue(self.allowed(cls, method=method))
            self.assertFalse(self.allowed(cls, method=method))
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(my_startup_views.ProfileView))
        self.assertFalse(self.allowed(my_startup_views.BalanceView))

    def test_account_budget_cannot_be_bypassed_by_company_device_or_scope_parameters(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(company="second", device="second"))
        self.assertFalse(self.allowed(company="third", device="third"))
        self.assertTrue(self.allowed(user=2))
        self.now += 61
        self.assertTrue(self.allowed())

    def test_startup_updates_and_my_startup_share_the_same_bounded_account_buckets(self):
        self.assertTrue(self.allowed(startup_views.BootstrapView))
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed(startup_views.BootstrapView))
        self.assertTrue(self.allowed(startup_views.ActiveRunView))
        self.assertTrue(self.allowed(my_startup_views.RunView))
        self.assertFalse(self.allowed(startup_views.RunView))
        self.assertTrue(self.allowed(my_startup_views.ProfileView, method="POST"))
        self.assertTrue(self.allowed(startup_views.SettingsView, method="PUT"))
        self.assertFalse(self.allowed(my_startup_views.SettingsView, method="PUT"))

    def test_http_429_has_retry_after_and_recovery_keeps_authentication_and_private_cache(self):
        factory = APIRequestFactory()
        user = Obj(pk=1, is_authenticated=True)

        def call(authenticated=True):
            request = factory.get("/api/v1/my-startup/auth/me/")
            if authenticated:
                force_authenticate(request, user=user)
            return my_startup_views.AccountView.as_view()(request)

        with patch.object(my_startup_views.CurrentUserView, "get", return_value=Response({"ok": True})) as domain:
            self.assertEqual(call().status_code, 200)
            self.assertEqual(call().status_code, 200)
            response = call()
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], "60")
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(domain.call_count, 2)
            self.assertEqual(call(authenticated=False).status_code, 401)
            self.now += 61
            self.assertEqual(call().status_code, 200)

    def test_alias_keeps_an_expensive_operation_specific_throttle(self):
        class ExpensiveView(APIView):
            throttle_classes = (ScopedRateThrottle,)
            throttle_scope = "expensive"

            def post(self, request):
                return Response({"ok": True})

        adapted = startup_view(ExpensiveView).view_class
        self.assertTrue(issubclass(adapted, MyStartupViewMixin))
        with patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"expensive": "1/minute"}):
            self.assertTrue(self.allowed(adapted, method="POST"))
            self.assertFalse(self.allowed(adapted, method="POST"))
        self.assertFalse(self.allowed(my_startup_views.SettingsView, method="PUT"))
        self.assertTrue(self.allowed())

    @override_settings(CORS_ALLOWED_ORIGINS=["https://chat.mlai.au"])
    def test_real_cors_middleware_exposes_throttle_retry_to_browser_and_native_clients(self):
        factory = APIRequestFactory()
        user = Obj(pk=1, is_authenticated=True)
        endpoint = DesktopAuthCorsMiddleware(CorsMiddleware(my_startup_views.AccountView.as_view()))
        with patch.object(my_startup_views.CurrentUserView, "get", return_value=Response({"ok": True})):
            for origin in ("https://chat.mlai.au", "tauri://localhost"):
                with self.subTest(origin=origin):
                    cache.clear()
                    preflight = factory.options("/api/v1/my-startup/auth/me/", HTTP_ORIGIN=origin,
                        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET", HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization")
                    response = endpoint(preflight)
                    self.assertIn(response.status_code, (200, 204))
                    self.assertEqual(response["Access-Control-Allow-Origin"], origin)
                    for expected in (200, 200, 429):
                        request = factory.get("/api/v1/my-startup/auth/me/", HTTP_ORIGIN=origin)
                        force_authenticate(request, user=user)
                        response = endpoint(request)
                        self.assertEqual(response.status_code, expected)
                    self.assertEqual(response["Retry-After"], "60")
                    self.assertEqual(response["Access-Control-Allow-Origin"], origin)
                    exposed = {item.strip().lower() for item in response["Access-Control-Expose-Headers"].split(",")}
                    self.assertTrue({"retry-after", "x-request-id"}.issubset(exposed))
                    if origin.startswith("tauri:"):
                        self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_every_explicit_and_authenticated_alias_route_has_a_startup_throttle(self):
        def callbacks(patterns):
            for pattern in patterns:
                if isinstance(pattern, URLResolver):
                    yield from callbacks(pattern.url_patterns)
                else:
                    yield pattern.callback.view_class

        for cls in set(callbacks(alias_urls.urlpatterns)) | {cls for _, cls in my_startup_urls.ROUTES}:
            if cls is CaptureRooLinkView:
                continue  # Deliberately unauthenticated origin-checked token capture.
            with self.subTest(view=cls.__name__):
                view = cls()
                view.request = self.request()
                self.assertIn(StartupScopedThrottle, [type(item) for item in view.get_throttles()])
                self.assertNotEqual(getattr(view, "community_chat_throttle_scope", None), "community_chat_home")

        routes = {
            "auth/me/": "bootstrap", "points/me/balance/": "bootstrap",
            "founder-tools/profile/": "bootstrap", "vibe-marketing/bootstrap/": "bootstrap",
            "vibe-marketing/runs/example-run": "poll",
            "vibe-marketing/github/repos": "read", "vibe-marketing/editorial-catalog/": "read",
        }
        for path, bucket in routes.items():
            with self.subTest(path=path):
                view = resolve("/api/v1/my-startup/" + path).func.view_class()
                view.request = self.request()
                self.assertEqual(view.startup_throttle_scope, f"my_startup_{bucket}")
                view.request = self.request(method="POST")
                self.assertEqual(view.startup_throttle_scope, "my_startup_write")

        # The allowlisted aliases remain bounded even when a facade shadows
        # their production URL, and their original JWT classes stay unchanged.
        for path in ("auth/me/", "points/me/balance/", "founder-tools/profile/", "vibe-marketing/bootstrap/"):
            with self.subTest(alias=path):
                view = resolve("/" + path, urlconf=alias_urls).func.view_class()
                view.request = self.request()
                self.assertEqual(view.startup_throttle_scope, "my_startup_bootstrap")
        self.assertNotIn(StartupScopedThrottle, [type(item) for item in my_startup_views.CurrentUserView().get_throttles()])
