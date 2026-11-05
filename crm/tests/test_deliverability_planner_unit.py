"""Pure audience and pacing invariants, without schema execution or provider traffic."""

from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from crm.deliverability import hygiene, planner, policy, volume

NOW = datetime(2026, 11, 9, 9, tzinfo=volume.MELBOURNE)


def stream(**fields):
    return SimpleNamespace(**{'pk': 'news', 'ramp_stage': 0, 'state': 'warming', 'daily_budget_override': None,
        'peak_healthy_daily_volume': 0, 'last_send_day': None, 'policy_overrides': {}, 'state_detail': {}, 'save': Mock(), **fields})


def contact(**fields):
    return SimpleNamespace(**{'email': 'member@example.net', 'attributes': {}, 'marketing_consent': 'granted',
        'marketing_consent_at': NOW - timedelta(days=400), 'last_active_at': None, 'soft_bounce_streak': 0, **fields})


class VolumeTests(SimpleTestCase):
    def test_five_thousand_people_finish_in_six_sending_days_without_catch_up(self):
        result = volume.estimate(stream(), 5000, NOW)
        self.assertEqual([row['count'] for row in result['schedule']], [150, 300, 600, 1000, 1500, 1450])
        self.assertEqual(result['days'], 6)
        self.assertEqual(result['schedule'][-1]['day'], '2026-11-16')
        by_hour = Counter()
        caps = {}
        for slot in result['slots']:
            clock = datetime.fromisoformat(slot['at'])
            self.assertLess(clock.weekday(), 5)
            self.assertTrue(9 <= clock.hour < 16)
            key = clock.replace(minute=0)
            by_hour[key] += slot['count']
            caps[key] = max(caps.get(key, 0), slot['hour_cap'])
        for key, count in by_hour.items():
            self.assertLessEqual(count, caps[key])
        self.assertEqual(result['slots'][0]['count'], 50)
        self.assertGreaterEqual(datetime.fromisoformat(result['slots'][1]['at']) - NOW, timedelta(minutes=45))

    def test_held_stream_has_no_promised_finish_and_no_catch_up_budget(self):
        held = volume.estimate(stream(state='held'), 5000, NOW)
        self.assertIsNone(held['finish_at'])
        self.assertEqual(held['schedule'], [])
        self.assertEqual(volume.budget(stream(state='held')), 0)
        self.assertEqual(volume.budget(stream(state='stopped')), 0)
        self.assertEqual(volume.budget(stream()), 150)

    def test_daily_override_and_active_peak_are_bounded(self):
        self.assertEqual(volume.budget(stream(daily_budget_override=100000)), 45000)
        self.assertEqual(volume.budget(stream(ramp_stage=3, state='slowed')), 500)
        self.assertEqual(volume.budget(stream(state='active', peak_healthy_daily_volume=30000)), 45000)

    def test_first_canary_cannot_exceed_daily_or_hourly_remaining_capacity(self):
        for budget in (1, 20, 50, 150, 300, 1000):
            for spent in (0, budget // 2, budget):
                n, cap = volume.slot_allowance(budget, spent, 0, 7, 7, canary=True)
                self.assertLessEqual(n, budget - spent)
                self.assertLessEqual(n, cap)
        self.assertEqual(volume.slot_allowance(150, 150, 0, 7, 1), (0, 33))

    def test_provider_throttles_do_not_reduce_unaffected_providers(self):
        normal = volume.provider_caps(100, {'google': 800, 'microsoft': 200})
        slowed = volume.provider_caps(100, {'google': 800, 'microsoft': 200}, {'microsoft': 'throttled'})
        held = volume.provider_caps(100, {'google': 800, 'microsoft': 200}, {'microsoft': 'held'})
        self.assertEqual(normal, {'google': 100, 'microsoft': 25})
        self.assertEqual(slowed, {'google': 100, 'microsoft': 7})
        self.assertEqual(held, {'google': 100, 'microsoft': 0})

    def test_idle_decay_uses_only_the_matching_gap_and_is_idempotent(self):
        self.assertEqual([volume.decay_stage(10, days) for days in (29, 30, 90, 180)], [10, 8, 5, 0])
        idle = stream(ramp_stage=10, last_send_day=NOW.date() - timedelta(days=90))
        with patch.object(volume.CrmAuditEvent, 'objects') as audit:
            volume.apply_idle_decay(idle, NOW)
            volume.apply_idle_decay(idle, NOW)
        self.assertEqual(idle.ramp_stage, 5)
        self.assertEqual(audit.create.call_count, 1)

    def test_weekends_and_blackouts_apply_before_capacity(self):
        saturday = NOW + timedelta(days=5)
        self.assertIsNone(volume.window(stream(), saturday))
        self.assertIsNotNone(volume.window(stream(), saturday, critical=True))
        with override_settings(CRM_BLACKOUT_DATES=(saturday.date().isoformat(),)):
            self.assertIsNone(volume.window(stream(), saturday, critical=True))

    def test_promotion_needs_health_and_sufficient_actual_use(self):
        self.assertEqual(volume.promote_stage(0, 90, 150, True), 1)
        self.assertEqual(volume.promote_stage(0, 89, 150, True), 0)
        self.assertEqual(volume.promote_stage(0, 150, 150, False), 0)
        row = SimpleNamespace(sent=150, delivered=150, bounced_hard=0, complained=0, deferred=0)
        self.assertFalse(volume.healthy_day(row, feedback=False))
        self.assertFalse(volume.healthy_day(row, incident=True))
        self.assertTrue(volume.healthy_day(row))

    def test_ladder_and_window_are_configurable_without_allowing_invalid_values(self):
        parsed = policy.environment_settings({'CRM_RAMP_LADDER': '100,200,400', 'CRM_HOURLY_HEADROOM': '1.25'})
        self.assertEqual(parsed['CRM_RAMP_LADDER'], (100, 200, 400))
        self.assertEqual(parsed['CRM_HOURLY_HEADROOM'], 1.25)
        for raw in ('', '100,50', '100,100', '0,10', 'not-a-number'):
            with self.assertRaises(ValueError):
                policy.environment_settings({'CRM_RAMP_LADDER': raw})
        with self.assertRaises(ValueError):
            volume.window_spec(stream(policy_overrides={'CRM_SEND_WINDOW': 'mon-fri 16:00-09:00 Australia/Melbourne'}))


class PlannerTests(SimpleTestCase):
    def test_activity_and_current_consent_determine_tiers_without_using_opens(self):
        self.assertEqual(planner.tier_for(contact(last_active_at=NOW - timedelta(days=10)), NOW, stars=0), 'A')
        self.assertEqual(planner.tier_for(contact(last_active_at=NOW - timedelta(days=60)), NOW, stars=0), 'B')
        self.assertEqual(planner.tier_for(contact(last_active_at=NOW - timedelta(days=200)), NOW, stars=0), 'C')
        self.assertEqual(planner.tier_for(contact(first_opened_at=NOW), NOW, stars=0), 'D')
        self.assertEqual(planner.tier_for(contact(marketing_consent_at=NOW - timedelta(days=1)), NOW, stars=0), 'A')
        self.assertEqual(planner.tier_for(contact(last_active_at=NOW + timedelta(days=1)), NOW, stars=0), 'D')
        for stars, tier in ((4, 'A'), (3, 'B'), (2, 'C')):
            self.assertEqual(planner.tier_for(contact(), NOW, stars=stars), tier)

    def test_interleaving_preserves_provider_mix_and_each_providers_rank_order(self):
        rows = [SimpleNamespace(provider='google' if index < 8 else 'microsoft', rank=index) for index in range(10)]
        ordered = planner.interleave(rows)
        self.assertEqual(Counter(row.provider for row in ordered), {'google': 8, 'microsoft': 2})
        self.assertEqual([row.rank for row in ordered if row.provider == 'google'], list(range(8)))
        self.assertEqual([row.rank for row in ordered if row.provider == 'microsoft'], [8, 9])
        self.assertIn('microsoft', [row.provider for row in ordered[:5]])
        self.assertEqual([row.rank for row in planner.interleave(rows)], [row.rank for row in ordered])

    def test_unknown_mx_is_not_reported_as_an_invalid_address(self):
        unknown = hygiene.DomainInfo('example.net', 'other', (), None, False, NOW)
        missing = hygiene.DomainInfo('example.net', 'other', (), False, False, NOW)
        self.assertEqual(hygiene.exclusion(contact(), unknown, 'marketing'), 'domain_unverified')
        self.assertEqual(hygiene.exclusion(contact(), missing, 'marketing'), 'no_mx')

    def test_hygiene_first_reason_and_typo_suggestions_never_edit_addresses(self):
        member = contact(email='noreply@gmail.co')
        info = hygiene.DomainInfo('gmail.co', 'other', ('10 mail.gmail.co',), True, False, NOW)
        self.assertEqual(hygiene.exclusion(member, info, 'marketing'), 'likely_typo')
        self.assertEqual(member.email, 'noreply@gmail.co')
        self.assertEqual(hygiene.exclusion(contact(email='person@gmial.com'), info, 'marketing'), 'disposable')
        role = contact(email='abuse@example.net')
        self.assertEqual(hygiene.exclusion(role, info, 'marketing'), 'role_address')
        self.assertIsNone(hygiene.exclusion(role, info, 'event_update'))
        self.assertIsNone(hygiene.exclusion(contact(email='info@example.net'), info, 'marketing'))

    def test_mx_classification_rejects_lookalike_suffixes(self):
        self.assertEqual(hygiene.provider_for_mx(['10 aspmx.l.google.com.']), 'google')
        self.assertEqual(hygiene.provider_for_mx(['10 tenant.mail.protection.outlook.com.']), 'microsoft')
        self.assertEqual(hygiene.provider_for_mx(['10 mx.fakegoogle.com.']), 'other')
        self.assertEqual(hygiene.provider_for_mx(['10 mx.mail.yahoo.com.evil.example.']), 'other')
