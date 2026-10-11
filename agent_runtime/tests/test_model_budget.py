"""Budget accounting without a database, provider call or deployment secret."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from agent_runtime import model_proxy, runtime
from agent_runtime.tests.test_core import NOW, task_fixture


@override_settings(AGENT_RUNTIME_MODEL_INPUT_MICROUNITS_PER_MILLION=1000000,
                   AGENT_RUNTIME_MODEL_OUTPUT_MICROUNITS_PER_MILLION=2000000)
class ModelBudgetTests(SimpleTestCase):
    def setUp(self):
        self.task = task_fixture()
        self.task.limits = {"max_model_calls": 3, "max_model_tokens": 1000, "max_cost_microunits": 1000}
        self.task.model_calls = 0
        self.task.model_tokens_used = 0
        self.task.reserved_model_tokens = 0
        self.principal = SimpleNamespace(task=self.task, credential=SimpleNamespace(lease_epoch=self.task.lease_epoch))
        self.enterContext(patch.object(model_proxy.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(model_proxy.timezone, "now", return_value=NOW))
        self.enterContext(patch.object(runtime, "_task_with_grant_lock", return_value=self.task))

    def test_ambiguous_provider_response_keeps_cost_and_token_reservation(self):
        reservation = model_proxy.reserve_budget(self.principal, 100, 20)
        self.assertEqual((self.task.model_calls, self.task.reserved_model_tokens, self.task.actual_cost_microunits), (1, 100, 120))
        self.task.save.reset_mock()
        for usage in (None, {}, {"total_tokens": True}, {"total_tokens": 101}, {"total_tokens": -1}):
            model_proxy.settle_budget(reservation, usage)
        self.assertEqual((self.task.reserved_model_tokens, self.task.actual_cost_microunits), (100, 120))
        self.task.save.assert_not_called()

    def test_valid_usage_releases_only_confirmed_reservation(self):
        reservation = model_proxy.reserve_budget(self.principal, 100, 20)
        model_proxy.settle_budget(reservation, {"total_tokens": 30, "prompt_tokens": 20, "completion_tokens": 10})
        self.assertEqual((self.task.model_calls, self.task.model_tokens_used, self.task.reserved_model_tokens,
                          self.task.actual_cost_microunits), (1, 30, 0, 40))

    def test_call_token_and_cost_limits_reject_before_charging(self):
        for field, value, code in (("model_calls", 3, "model_budget_exhausted"),
                                   ("reserved_model_tokens", 950, "model_budget_exhausted"),
                                   ("actual_cost_microunits", 950, "model_cost_budget_exhausted")):
            original = getattr(self.task, field)
            setattr(self.task, field, value)
            self.task.save.reset_mock()
            with self.subTest(field=field), self.assertRaisesRegex(model_proxy.ModelProxyError, code):
                model_proxy.reserve_budget(self.principal, 100, 20)
            self.task.save.assert_not_called()
            setattr(self.task, field, original)

    def test_lost_lease_and_missing_pricing_never_admit_call(self):
        self.task.lease_epoch += 1
        with self.assertRaisesRegex(model_proxy.ModelProxyError, "task_authority_expired"):
            model_proxy.reserve_budget(self.principal, 100, 20)
        self.task.save.assert_not_called()
        with override_settings(AGENT_RUNTIME_MODEL_INPUT_MICROUNITS_PER_MILLION=0):
            with self.assertRaisesRegex(model_proxy.ModelProxyError, "model_pricing_not_configured"):
                model_proxy.reserve_budget(self.principal, 100, 20)

    def test_untrustworthy_usage_breakdown_keeps_full_cost(self):
        reservation = model_proxy.reserve_budget(self.principal, 100, 20)
        model_proxy.settle_budget(reservation, {"total_tokens": 30, "prompt_tokens": 1, "completion_tokens": 1})
        self.assertEqual(self.task.actual_cost_microunits, 120)
        self.assertEqual(self.task.model_tokens_used, 30)
