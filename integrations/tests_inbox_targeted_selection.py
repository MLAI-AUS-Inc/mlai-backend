"""Database-free scheduling and provider-admission regressions."""
from django.test import SimpleTestCase, override_settings
from unittest.mock import MagicMock, patch
from integrations.services.slack_chat_read_state import ReadTarget
from integrations.services.message_sync import read_priority as priority
from integrations.services.message_sync.targeted_reads import possibly_unread


class ScopedCanarySelectionTests(SimpleTestCase):
    def test_owner_scope_narrows_the_due_grant_query(self):
        from integrations.services.message_sync import read_state
        rows = MagicMock()
        rows.filter.return_value = rows
        rows.values.return_value.annotate.return_value.order_by.return_value = []
        with patch.object(read_state.SlackDmMirrorGrant.objects, 'filter', return_value=rows):
            self.assertIsNone(read_state.claim_read_state(owner_id=1))
        rows.filter.assert_any_call(user_id=1)

    def test_malformed_owner_cannot_expand_the_canary(self):
        from integrations.services.message_sync import read_state
        for owner in (True, 0, -1, '1'):
            with self.assertRaises(ValueError):
                read_state.claim_read_state(owner_id=owner)

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_scoped_refresh_passes_the_owner_to_lease_selection(self):
        from integrations.services.message_sync import read_state
        with patch.object(read_state, 'claim_read_state', return_value=None) as claim:
            self.assertEqual(read_state.refresh_read_state_once(owner_id=1), 0)
        claim.assert_called_once_with(owner_id=1)


@override_settings(MESSAGE_SYNC_TARGETED_READ_POLLING=True, READ_STATE_SAFETY_SWEEP_HOURS=6)
class TargetedSelectionTests(SimpleTestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.targets = [ReadTarget(name, name, 'im', source_activity_ts=str(self.now - 10))
                        for name in ['visible', 'possible', 'known', 'quiet']]
        self.states = {
            'visible': {'fetched_at': self.now - 20, 'last_read': str(self.now)},
            'possible': {'fetched_at': self.now - 40, 'last_read': str(self.now - 50)},
            'known': {'fetched_at': self.now - 80, 'last_read': str(self.now), 'is_unread': True},
            'quiet': {'fetched_at': self.now - 7 * 3600, 'last_read': str(self.now)},
        }
        self.cursor = {priority.KEY: {'visible': {'until': self.now + 90, 'reason': 'visible'}}}

    def choose(self, turn=0):
        return priority.select_target(self.targets, self.states, lambda t: t.slack_id,
                                      self.cursor, now=self.now, turn=turn)

    def test_tiers_and_spare_safety_budget(self):
        self.assertEqual(self.choose().slack_id, 'visible')
        self.cursor = {}
        self.assertEqual(self.choose().slack_id, 'possible')
        self.states['possible']['fetched_at'] = self.now
        self.assertEqual(self.choose().slack_id, 'known')
        self.states['known']['fetched_at'] = self.now
        self.assertEqual(self.choose().slack_id, 'quiet')

    def test_hot_visible_work_cannot_starve_safety(self):
        choices = [self.choose(turn).slack_id for turn in range(30)]
        self.assertEqual(choices.count('quiet'), 3)
        self.assertEqual(choices.count('known'), 9)
        self.assertEqual(choices.count('visible'), 18)

    def test_continuous_hints_and_cache_repairs_cannot_starve_confirmed_risk(self):
        self.states['quiet']['refresh_required'] = True
        self.cursor[priority.KEY]['known'] = {
            'until': self.now + 300, 'reason': 'own_message',
        }
        self.states['known']['fetched_at'] = self.now - 2
        choices = [self.choose(turn).slack_id for turn in range(30)]
        self.assertEqual(choices.count('quiet'), 3)
        self.assertEqual(choices.count('possible'), 9)
        self.assertEqual(choices.count('known'), 18)

    def test_reserved_risk_rotates_oldest_first_and_retains_retry_after(self):
        self.states['known']['fetched_at'] = self.now
        first = self.targets[1]
        second = ReadTarget('second', 'second', 'im', source_activity_ts=str(self.now - 10))
        self.targets.append(second)
        self.states['second'] = {'fetched_at': self.now - 35, 'last_read': str(self.now - 50)}
        self.assertEqual(self.choose(2), first)
        self.states[first.slack_id]['fetched_at'] = self.now
        self.assertEqual(self.choose(5), second)
        self.cursor['message_sync_read_state_v1'] = {'retries': {'second': self.now + 60}}
        self.assertEqual(self.choose(8).slack_id, 'visible')

    def test_retry_after_is_not_bypassed_by_safety_or_hints(self):
        self.cursor['message_sync_read_state_v1'] = {'retries': {'visible': self.now + 60, 'quiet': self.now + 120}}
        self.assertEqual(self.choose(9).slack_id, 'possible')

    def test_quiet_fresh_targets_do_not_receive_directory_sweeps(self):
        for state in self.states.values():
            state.update(last_read=str(self.now), fetched_at=self.now - 100, is_unread=False)
        self.cursor = {}
        self.assertIsNone(self.choose())

    def test_baseline_and_exclusion_still_get_safety_checks(self):
        self.states['quiet'] = {'available': False, 'excluded': True}
        self.assertEqual(self.choose(9).slack_id, 'quiet')

    def test_possibly_unread_requires_finite_source_ordering(self):
        for value in ('nan', 'inf', '-1', str(self.now + 301), ''):
            target = ReadTarget('x', 'x', 'im', source_activity_ts=value)
            self.assertFalse(possibly_unread(target, {}, now=self.now))
        self.assertFalse(possibly_unread(self.targets[1], {'excluded': True}, now=self.now))

    def test_own_message_hint_is_prompt_but_not_a_read(self):
        self.states['known']['fetched_at'] = self.now - 2
        self.cursor = {priority.KEY: {'known': {'until': self.now + 300, 'reason': 'own_message'}}}
        self.assertEqual(self.choose().slack_id, 'known')
        self.assertTrue(self.states['known']['is_unread'])

    def test_own_message_hint_precedes_older_unhinted_refresh_work(self):
        self.states['known']['fetched_at'] = self.now - 2
        self.states['quiet']['refresh_required'] = True
        self.cursor = {priority.KEY: {'known': {'until': self.now + 300, 'reason': 'own_message'}}}
        self.assertEqual(self.choose().slack_id, 'known')
        self.assertEqual(self.choose(9).slack_id, 'quiet')

    def test_visible_hint_precedes_older_unhinted_refresh_work(self):
        self.states['quiet']['refresh_required'] = True
        self.assertEqual(self.choose().slack_id, 'visible')

    def test_own_message_precedes_older_visibility_without_skipping_safety(self):
        self.states['known']['fetched_at'] = self.now - 2
        self.cursor[priority.KEY]['known'] = {'until': self.now + 300, 'reason': 'own_message'}
        self.assertEqual(self.choose().slack_id, 'known')
        self.assertEqual(self.choose(9).slack_id, 'quiet')
        self.states['known']['fetched_at'] = self.now
        self.assertEqual(self.choose().slack_id, 'visible')

    def test_hinted_priority_keeps_age_and_retry_fences(self):
        self.states['known']['fetched_at'] = self.now
        self.states['quiet']['refresh_required'] = True
        self.cursor = {priority.KEY: {'known': {'until': self.now + 300, 'reason': 'own_message'}}}
        self.assertEqual(self.choose().slack_id, 'quiet')
        self.states['known']['fetched_at'] = self.now - 2
        self.cursor['message_sync_read_state_v1'] = {'retries': {'known': self.now + 60}}
        self.assertEqual(self.choose().slack_id, 'quiet')

    def test_unhinted_refresh_keeps_priority_over_possible_work(self):
        self.cursor = {}
        self.states['quiet']['refresh_required'] = True
        self.assertEqual(self.choose().slack_id, 'quiet')

    def test_selected_possible_work_gets_foreground_provider_admission(self):
        from integrations.services.message_sync.read_state import refresh_request_priority
        self.assertEqual(refresh_request_priority(self.targets[1], self.states['possible'], {}, now=self.now), 'foreground')
