"""Import the actual profile in a clean process with DB/network/dotenv denied."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from .test_founder_staging_environment import environment


class FounderProfileTests(unittest.TestCase):
    def test_real_profile_loads_without_dotenv_or_shared_destinations(self):
        code = '''
import json
from unittest.mock import patch
with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), \\
     patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \\
     patch("django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection", side_effect=AssertionError("database forbidden")):
    import mlai.settings_staging_founder as s
    import django
    django.setup()
    import core.firebase_utils, core.email_utils
    from community_chat.founder_staging_email import fixture_scope
    from mlai.staging_integrations import is_isolated_staging
    scope = fixture_scope()
    assert scope.domain == "staging.founder.mlai.au" and is_isolated_staging()
    print(json.dumps({"debug": s.DEBUG, "hosts": s.ALLOWED_HOSTS,
        "db": [s.DATABASES["default"][key] for key in ("HOST", "PORT", "NAME", "USER")],
        "atomic": s.DATABASES["default"]["ATOMIC_REQUESTS"],
        "reporter": s.TOKEN_USAGE_API_BASE, "adapter": s.COMMUNITY_CHAT_ADAPTER_URL,
        "relay": s.COMMUNITY_CHAT_RELAY_URL, "origins": s.COMMUNITY_CHAT_ALLOWED_ORIGINS,
        "access_cookie": s.COMMUNITY_CHAT_ACCESS_COOKIE_NAME, "refresh_cookie": s.COMMUNITY_CHAT_REFRESH_COOKIE_NAME,
        "host_only": s.COMMUNITY_CHAT_HOST_ONLY_COOKIES, "clear_old": s.COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES,
        "member_accounts": s.COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED,
        "native": [s.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED, s.COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED],
        "federation": [s.COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED, s.COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED],
        "capabilities": s.STAGING_CAPABILITIES,
        "notifications": [getattr(s, key) for key in ("ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED", "ARTICLES_GOAL_NOTIFICATIONS_ENABLED", "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", "MY_STARTUP_TURN_REMINDERS_ENABLED")]}))
'''
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG"}}
        result = subprocess.run([sys.executable, "-c", code], env={**env, **environment()},
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr[-1600:])
        value = json.loads(result.stdout)
        self.assertFalse(value["debug"])
        self.assertEqual(value["hosts"], ["api-founder-staging.mlai.au"])
        self.assertEqual(value["db"], ["founder-postgres", 5432, "founder_acceptance", "founder_runtime"])
        self.assertFalse(value["atomic"])
        self.assertEqual(value["adapter"], "http://founder-chat-adapter:3100")
        self.assertEqual(value["reporter"], "https://api-founder-staging.mlai.au/api/v1/community-chat/usage")
        self.assertEqual(value["relay"], "wss://founder-staging.mlai.au")
        self.assertEqual(value["origins"], ["https://founder-staging.mlai.au"])
        self.assertEqual(value["access_cookie"], "__Host-mlai_founder_access")
        self.assertEqual(value["refresh_cookie"], "__Host-mlai_founder_refresh")
        self.assertTrue(value["host_only"])
        self.assertFalse(value["clear_old"])
        self.assertTrue(value["member_accounts"])
        self.assertEqual(value["native"], [False, False])
        self.assertEqual(value["federation"], [False, False])
        self.assertEqual(value["notifications"], [False] * 4)
        self.assertEqual(value["capabilities"], {"email": "local", "account_creation": "local"})


if __name__ == "__main__":
    unittest.main()
