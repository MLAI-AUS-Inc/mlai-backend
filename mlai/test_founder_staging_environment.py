"""DB-free early-startup refusal controls for the proposed separate runtime."""
import unittest

from .founder_staging_environment import checked_founder_environment, InvalidFounderEnvironment


def environment():
    return {"DJANGO_SETTINGS_MODULE": "mlai.settings_staging_founder", "APP_ENV": "chat-staging",
            "FOUNDER_STAGING_ENABLED": "true", "FOUNDER_STAGING_INBOX_PROVIDER_ENABLED": "true",
            "RUN_MIGRATIONS_ON_START": "0", "DATABASE_URL": "postgresql://founder_runtime:" + "a" * 64 + "@founder-postgres:5432/founder_acceptance",
            "REDIS_URL": "redis://:" + "b" * 64 + "@founder-redis:6379/0", "SECRET_KEY": "c" * 96,
            "COMMUNITY_CHAT_EMAIL_CODE_PEPPER": "d" * 64, "FOUNDER_STAGING_INBOX_WRITE_TOKEN": "e" * 64,
            "COMMUNITY_CHAT_ADAPTER_TOKEN": "f" * 64, "COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET": "0" * 64,
            "MLAI_CHAT_ACCOUNT_KEY_SECRET": "1" * 64, "FOUNDER_STAGING_EMAIL_DOMAIN": "staging.founder.mlai.au"}


class StartupBoundaryTests(unittest.TestCase):
    def test_explicit_separate_fixture_inputs_and_private_repr(self):
        value = checked_founder_environment(environment())
        self.assertEqual(value.email_domain, "staging.founder.mlai.au")
        self.assertNotIn("c" * 96, repr(value))
        self.assertNotIn("postgresql", repr(value))

    def test_missing_activation_or_wrong_profile_is_refused(self):
        for key, value in (("DJANGO_SETTINGS_MODULE", "mlai.settings"), ("APP_ENV", "production"),
                           ("FOUNDER_STAGING_ENABLED", "false"), ("FOUNDER_STAGING_INBOX_PROVIDER_ENABLED", "false"),
                           ("RUN_MIGRATIONS_ON_START", "1")):
            with self.subTest(key=key), self.assertRaises(InvalidFounderEnvironment):
                checked_founder_environment({**environment(), key: value})

    def test_old_pilot_production_and_routing_overrides_are_refused(self):
        original = environment()["DATABASE_URL"]
        for value in (original.replace("founder-postgres", "postgres-api"), original.replace("founder_runtime", "staging_account_api"),
                      original.replace("founder_acceptance", "staging_account_api"), original + "?hostaddr=172.28.240.2",
                      original + "#host=old", original.replace(":5432/", ":0/"), original.replace(":5432/", ":99999/")):
            with self.subTest(route=value.rsplit("@", 1)[-1]), self.assertRaises(InvalidFounderEnvironment):
                checked_founder_environment({**environment(), "DATABASE_URL": value})
        for name in ("PGHOST", "PGHOSTADDR", "PGOPTIONS", "PGSERVICE", "PGSERVICEFILE"):
            with self.subTest(key=name), self.assertRaises(InvalidFounderEnvironment):
                checked_founder_environment({**environment(), name: "unapproved"})

    def test_ambient_credentials_and_reader_token_are_refused(self):
        for name in ("OPENAI_API_KEY", "RESEND_API_KEY", "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS",
                     "FOUNDER_STAGING_INBOX_READ_TOKEN", "SLACK_BOT_TOKEN", "CONTENT_FACTORY_URL",
                     "FIREBASE_PRIVATE_KEY", "FIREBASE_PRIVATE_KEY_ID", "FIREBASE_CLIENT_EMAIL", "STRIPE_API_KEY",
                     "STRIPE_API_SECRET_KEY", "STRIPE_VIBE_RAISING_KEY", "STRIPE_VIBE_RAISING_SECRET_KEY",
                     "GITHUB_APP_PRIVATE_KEY", "GOOGLE_OAUTH_CLIENT_SECRET", "FT_SLACK_CLIENT_SECRET", "TWILIO_AUTH_TOKEN",
                     "AGENT_RUNTIME_MODEL_API_KEY", "FIELD_ENCRYPTION_KEY", "LEGACY_FIELD_ENCRYPTION_SECRET",
                     "DJANGO_SECRET_KEY", "CONNECTOR_CREDENTIAL_KEYS", "DATAFORSEO_API_KEY_B64"):
            with self.subTest(key=name), self.assertRaises(InvalidFounderEnvironment):
                checked_founder_environment({**environment(), name: "unapproved"})

    def test_token_limits_and_ttl_names_are_not_credentials(self):
        value = checked_founder_environment({**environment(), "COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS": "72",
                                            "ARTICLES_RUN_CONTROL_MODEL_TOKENS": "0"})
        self.assertEqual(value.email_domain, "staging.founder.mlai.au")

    def test_private_inputs_cannot_be_reused_or_revealed_in_errors(self):
        value = environment(); value["FOUNDER_STAGING_INBOX_WRITE_TOKEN"] = value["COMMUNITY_CHAT_ADAPTER_TOKEN"]
        with self.assertRaises(InvalidFounderEnvironment) as failure:
            checked_founder_environment(value)
        self.assertNotIn("f" * 64, str(failure.exception))
        value = environment(); value["DATABASE_URL"] = value["DATABASE_URL"].replace("a" * 64, "short")
        with self.assertRaises(InvalidFounderEnvironment):
            checked_founder_environment(value)
        for name in ("COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET", "MLAI_CHAT_ACCOUNT_KEY_SECRET"):
            with self.subTest(name=name), self.assertRaises(InvalidFounderEnvironment):
                checked_founder_environment({**environment(), name: environment()["SECRET_KEY"]})

    def test_cache_notifications_password_and_email_domain_are_fixed(self):
        for key, value in (("REDIS_URL", environment()["REDIS_URL"].replace("founder-redis", "redis-api")),
                           ("ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED", "true"),
                           ("COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED", "true"),
                           ("FOUNDER_STAGING_EMAIL_DOMAIN", "mlai.au")):
            with self.subTest(key=key), self.assertRaises(InvalidFounderEnvironment):
                checked_founder_environment({**environment(), key: value})


if __name__ == "__main__":
    unittest.main()
