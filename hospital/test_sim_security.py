from datetime import datetime, timedelta, timezone as datetime_timezone
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import math
import time
from unittest import mock
from unittest.mock import patch
import uuid

from django.core.management import call_command
from django.core.cache import cache
from django.core.management.base import CommandError
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone

from mlai.settings import (
    _build_cache_settings,
    _validate_health_hack_ai_modes,
    _validate_health_hack_service_secrets,
)

from .models import (
    SimCaseWinner,
    SimConversation,
    SimConversationTurn,
    SimDiagnosisGuess,
    SimParticipant,
)
from .sim_retention import run_scheduled_sim_conversation_cleanup
from .sim_security import (
    BudgetReservation,
    _daily_key,
    _daily_timeout,
    _reserve_budget_atomic,
)


@override_settings(HEALTH_HACK_CHAT_RETENTION_DAYS=30)
class CleanupSimConversationsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.participant = SimParticipant.objects.create(id=uuid.uuid4())

    def make_turn(self, *, age_days, role="patient", message="question"):
        conversation, _ = SimConversation.objects.get_or_create(
            participant=self.participant,
            case_id=1,
            role=role,
        )
        turn = SimConversationTurn.objects.create(
            conversation=conversation,
            message_id=uuid.uuid4(),
            player_text=message,
            npc_text="answer",
            response_source=SimConversationTurn.SOURCE_LLM,
            completed_at=timezone.now(),
        )
        created_at = timezone.now() - timedelta(days=age_days)
        SimConversationTurn.objects.filter(pk=turn.pk).update(created_at=created_at)
        SimConversation.objects.filter(pk=conversation.pk).update(last_turn_at=created_at)
        turn.refresh_from_db()
        return turn

    def test_deletes_only_expired_dialogue_and_empty_shells(self):
        expired = self.make_turn(age_days=31, role="patient")
        recent = self.make_turn(age_days=2, role="nurse")
        output = StringIO()

        call_command("cleanup_sim_conversations", stdout=output)

        self.assertFalse(SimConversationTurn.objects.filter(pk=expired.pk).exists())
        self.assertTrue(SimConversationTurn.objects.filter(pk=recent.pk).exists())
        self.assertFalse(
            SimConversation.objects.filter(pk=expired.conversation_id).exists()
        )
        self.assertTrue(SimConversation.objects.filter(pk=recent.conversation_id).exists())
        self.assertIn("deleted_turns=1", output.getvalue())

    def test_preserves_contest_and_prize_rows(self):
        self.make_turn(age_days=31)
        guess = SimDiagnosisGuess.objects.create(
            case_id=1,
            client_id=str(self.participant.id),
            participant=self.participant,
            guess_text="adrenal crisis",
            is_correct=True,
            outcome=SimDiagnosisGuess.OUTCOME_TICKET,
            prize_kind=SimDiagnosisGuess.PRIZE_FREE_TICKET,
            email="winner@example.com",
        )
        winner = SimCaseWinner.objects.create(case_id=1, guess=guess)

        call_command("cleanup_sim_conversations")

        self.assertTrue(SimDiagnosisGuess.objects.filter(pk=guess.pk).exists())
        self.assertTrue(SimCaseWinner.objects.filter(pk=winner.pk).exists())
        self.assertTrue(SimParticipant.objects.filter(pk=self.participant.pk).exists())

    def test_dry_run_and_days_override(self):
        turn = self.make_turn(age_days=10)
        output = StringIO()

        call_command(
            "cleanup_sim_conversations",
            days=7,
            dry_run=True,
            stdout=output,
        )

        self.assertTrue(SimConversationTurn.objects.filter(pk=turn.pk).exists())
        self.assertIn("dry-run: expired_turns=1", output.getvalue())

    def test_rejects_zero_retention(self):
        with self.assertRaisesMessage(CommandError, "retention days must be at least 1"):
            call_command("cleanup_sim_conversations", days=0)


class ScheduledSimConversationCleanupTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch("hospital.sim_retention.call_command")
    def test_scheduler_runs_cleanup_only_once_per_local_day(self, command):
        now = timezone.now()
        first = run_scheduled_sim_conversation_cleanup(now=now)
        second = run_scheduled_sim_conversation_cleanup(now=now)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second, {
            "status": "skipped",
            "reason": "already_completed",
            "date": first["date"],
        })
        command.assert_called_once_with("cleanup_sim_conversations", stdout=mock.ANY)

    @patch("hospital.sim_retention.call_command")
    def test_failed_cleanup_can_retry(self, command):
        command.side_effect = [RuntimeError("database busy"), None]
        now = timezone.now()

        with self.assertRaises(RuntimeError):
            run_scheduled_sim_conversation_cleanup(now=now)
        result = run_scheduled_sim_conversation_cleanup(now=now)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(command.call_count, 2)


class SecurityBudgetClockTests(TestCase):
    @override_settings(TIME_ZONE="Australia/Melbourne")
    @patch("hospital.sim_security.timezone.now")
    def test_daily_budget_ttl_uses_local_midnight_before_utc_rollover(self, now):
        # 15:30 UTC on 12 July is 01:30 on 13 July in Melbourne. Computing
        # tomorrow from the UTC date would incorrectly produce an expired
        # local-midnight TTL and reset the budget every minute.
        now.return_value = datetime(
            2026,
            7,
            12,
            15,
            30,
            tzinfo=datetime_timezone.utc,
        )

        timeout = _daily_timeout()

        self.assertGreater(timeout, 22 * 60 * 60)
        self.assertLess(timeout, 23 * 60 * 60)

    def test_atomic_reservations_never_admit_beyond_hard_bound(self):
        cache.clear()

        def reserve(_index):
            return _reserve_budget_atomic(
                call_limit=5,
                token_limit=10,
                token_reservation=2,
                timeout=60,
                enforce=True,
            )[0]

        with ThreadPoolExecutor(max_workers=10) as executor:
            admitted = list(executor.map(reserve, range(20)))

        self.assertEqual(sum(admitted), 5)
        self.assertEqual(cache.get(_daily_key("calls")), 5)
        self.assertEqual(cache.get(_daily_key("tokens")), 10)

    def test_reservation_reconciliation_stays_bound_to_original_day(self):
        cache.clear()
        old_calls = _daily_key("calls", "2026-07-13")
        old_tokens = _daily_key("tokens", "2026-07-13")
        new_calls = _daily_key("calls", "2026-07-14")
        new_tokens = _daily_key("tokens", "2026-07-14")
        cache.set(old_calls, 1, timeout=60)
        cache.set(old_tokens, 20, timeout=60)
        cache.set(new_calls, 3, timeout=60)
        cache.set(new_tokens, 60, timeout=60)
        reservation = BudgetReservation(
            20,
            calls_key=old_calls,
            tokens_key=old_tokens,
            expires_at=math.ceil(time.time()) + 60,
        )

        reservation.reconcile(4, 6)

        self.assertEqual(cache.get(old_calls), 1)
        self.assertEqual(cache.get(old_tokens), 10)
        self.assertEqual(cache.get(new_calls), 3)
        self.assertEqual(cache.get(new_tokens), 60)


class SharedCacheConfigurationTests(TestCase):
    def test_production_requires_redis_url_and_dependency(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "REDIS_URL must be configured"):
            _build_cache_settings(
                redis_url="",
                redis_available=False,
                is_production=True,
            )
        with self.assertRaisesMessage(ImproperlyConfigured, "redis package is required"):
            _build_cache_settings(
                redis_url="redis://cache.internal:6379/0",
                redis_available=False,
                is_production=True,
            )

    def test_local_environment_may_use_process_local_cache(self):
        config = _build_cache_settings(
            redis_url="",
            redis_available=False,
            is_production=False,
        )
        self.assertEqual(
            config["default"]["BACKEND"],
            "django.core.cache.backends.locmem.LocMemCache",
        )

    @override_settings(CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    })
    def test_deployment_check_rejects_process_local_cache(self):
        with self.assertRaisesMessage(CommandError, "default cache must use"):
            call_command("validate_health_hack_ai_cache", stdout=StringIO())

    @override_settings(CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": "rediss://cache.internal:25061/0",
        },
    })
    @patch("hospital.management.commands.validate_health_hack_ai_cache.cache")
    def test_deployment_check_exercises_atomic_cache_operations(self, mock_cache):
        mock_cache.add.return_value = True
        mock_cache.incr.return_value = 2
        mock_cache.get.return_value = 2
        output = StringIO()

        call_command("validate_health_hack_ai_cache", stdout=output)

        self.assertIn("shared Redis cache is ready", output.getvalue())
        mock_cache.delete.assert_called_once()

    def test_production_service_secrets_are_strong_and_distinct(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "HEALTH_HACK_API_KEY"):
            _validate_health_hack_service_secrets(
                health_hack_key="short",
                roo_sim_patient_key="r" * 32,
                roo_api_key="a" * 32,
                is_production=True,
            )
        with self.assertRaisesMessage(ImproperlyConfigured, "must be distinct"):
            _validate_health_hack_service_secrets(
                health_hack_key="s" * 32,
                roo_sim_patient_key="s" * 32,
                roo_api_key="a" * 32,
                is_production=True,
            )
        with self.assertRaisesMessage(ImproperlyConfigured, "ROO_API_KEY"):
            _validate_health_hack_service_secrets(
                health_hack_key="h" * 32,
                roo_sim_patient_key="r" * 32,
                roo_api_key="short",
                is_production=True,
            )

        _validate_health_hack_service_secrets(
            health_hack_key="h" * 32,
            roo_sim_patient_key="r" * 32,
            roo_api_key="a" * 32,
            is_production=True,
        )

    def test_local_service_secrets_remain_optional(self):
        _validate_health_hack_service_secrets(
            health_hack_key="",
            roo_sim_patient_key="",
            roo_api_key="",
            is_production=False,
        )

    def test_global_budget_cannot_be_observation_only_in_production(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "must be enforce"):
            _validate_health_hack_ai_modes(
                rate_mode="observe",
                budget_mode="observe",
                is_production=True,
            )
        _validate_health_hack_ai_modes(
            rate_mode="observe",
            budget_mode="enforce",
            is_production=True,
        )
