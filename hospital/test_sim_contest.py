"""
Tests for the web ward-game diagnosis contest endpoints
(hospital/sim_contest_views.py).
"""
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import SimCaseWinner, SimDiagnosisGuess, SimParticipant

RECORD_URL = '/api/v1/hackathons/hospital/sim-guess/record/'
CLAIM_URL = '/api/v1/hackathons/hospital/sim-guess/claim/'
STATUS_URL = '/api/v1/hackathons/hospital/sim-guess/status/'

ROO_KEY = 'test-roo-key'
HEALTH_HACK_KEY = 'test-health-hack-key'
CLIENT_A = 'aaaaaaaa-1111-4111-8111-111111111111'
CLIENT_B = 'bbbbbbbb-2222-4222-8222-222222222222'
CLIENT_C = 'cccccccc-3333-4333-8333-333333333333'


@override_settings(ROO_API_KEY=ROO_KEY, HEALTH_HACK_API_KEY=HEALTH_HACK_KEY)
class SimGuessRecordTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle + any view caches
        self.client = APIClient()

    def _record(self, *, case_id=1, client_id=CLIENT_A, guess_text='adrenal crisis',
                is_correct=True, case_title='Salt & Static', key=ROO_KEY):
        kwargs = {}
        if key is not None:
            kwargs['HTTP_X_API_KEY'] = key
        payload = {
            'case_id': case_id,
            'client_id': client_id,
            'guess_text': guess_text,
            'is_correct': is_correct,
        }
        if case_title is not None:
            payload['case_title'] = case_title
        return self.client.post(RECORD_URL, payload, format='json', **kwargs)

    def test_record_requires_service_key(self):
        resp = self._record(key=None)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(SimDiagnosisGuess.objects.count(), 0)

        resp = self._record(key='wrong-key')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(SimDiagnosisGuess.objects.count(), 0)

    def test_record_correct_guess_creates_pending_claim(self):
        resp = self._record(is_correct=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {
            'already_guessed': False,
            'is_correct': True,
            'outcome': 'pending_claim',
            'prize_kind': 'free_ticket',
            'is_first_solver': True,
            'winner_taken': True,
        })
        guess = SimDiagnosisGuess.objects.get()
        self.assertEqual(guess.case_id, 1)
        self.assertEqual(guess.case_title, 'Salt & Static')
        self.assertEqual(guess.client_id, CLIENT_A)
        self.assertEqual(guess.outcome, 'pending_claim')
        self.assertEqual(guess.prize_kind, 'free_ticket')
        self.assertEqual(guess.email, '')
        self.assertEqual(SimCaseWinner.objects.get().guess, guess)

    def test_record_incorrect_guess_locks_out(self):
        resp = self._record(is_correct=False, guess_text='gastro')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['outcome'], 'incorrect')
        self.assertFalse(resp.data['is_correct'])

    @override_settings(HEALTH_HACK_ACTIVE_CASE_ID=7)
    def test_record_without_title_uses_rolling_deploy_fallback(self):
        resp = self._record(case_id=7, case_title=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SimDiagnosisGuess.objects.get().case_title, 'Case 7')

    def test_duplicate_guess_returns_stored_verdict(self):
        self._record(is_correct=False, guess_text='gastro')
        # A re-submission claiming to be correct must NOT upgrade the burnt guess.
        resp = self._record(is_correct=True, guess_text='adrenal crisis')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['already_guessed'])
        self.assertFalse(resp.data['is_correct'])
        self.assertEqual(resp.data['outcome'], 'incorrect')
        self.assertEqual(SimDiagnosisGuess.objects.count(), 1)

    def test_winner_taken_flag(self):
        self._record(client_id=CLIENT_A, is_correct=True)

        resp = self._record(client_id=CLIENT_B, is_correct=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['winner_taken'])
        self.assertFalse(resp.data['is_first_solver'])
        self.assertEqual(resp.data['prize_kind'], 'discount_30')
        # Different case → fresh winner slot.
        with self.settings(HEALTH_HACK_ACTIVE_CASE_ID=2):
            resp = self._record(case_id=2, client_id=CLIENT_B, is_correct=True)
        self.assertTrue(resp.data['winner_taken'])
        self.assertTrue(resp.data['is_first_solver'])

    def test_record_rejects_non_open_case(self):
        # Cases 1 and 2 are open by default (two-patient ward); anything else
        # is refused before a row can exist.
        resp = self._record(case_id=9)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'inactive_case')
        self.assertEqual(SimDiagnosisGuess.objects.count(), 0)

    def test_record_validation(self):
        resp = self._record(client_id='short')  # too short / bad format
        self.assertEqual(resp.status_code, 400)
        resp = self._record(guess_text='x' * 301)
        self.assertEqual(resp.status_code, 400)
        resp = self._record(case_id=0)
        self.assertEqual(resp.status_code, 400)
        resp = self._record(case_title='x' * 201)
        self.assertEqual(resp.status_code, 400)


@override_settings(
    ROO_API_KEY=ROO_KEY,
    HEALTH_HACK_API_KEY=HEALTH_HACK_KEY,
    HEALTH_HACK_FREE_TICKET_URL='https://luma.test/free',
    HEALTH_HACK_DISCOUNT_URL='https://luma.test/discount',
    # These tests exercise the flat fallback; the per-case coupon map has its
    # own class below.
    HEALTH_HACK_FREE_TICKET_URLS={},
)
class SimGuessClaimTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _seed_guess(self, *, case_id=1, client_id=CLIENT_A, is_correct=True,
                    prize_kind=None):
        if prize_kind is None:
            prize_kind = ('free_ticket' if is_correct and
                          not SimCaseWinner.objects.filter(case_id=case_id).exists()
                          else 'discount_30' if is_correct else 'none')
        guess = SimDiagnosisGuess.objects.create(
            case_id=case_id,
            client_id=client_id,
            guess_text='adrenal crisis' if is_correct else 'gastro',
            is_correct=is_correct,
            outcome=(SimDiagnosisGuess.OUTCOME_PENDING_CLAIM if is_correct
                     else SimDiagnosisGuess.OUTCOME_INCORRECT),
            prize_kind=prize_kind,
        )
        if prize_kind == 'free_ticket':
            SimCaseWinner.objects.create(case_id=case_id, guess=guess)
        return guess

    def _claim(self, *, case_id=1, client_id=CLIENT_A, email='doc@example.com'):
        return self.client.post(CLAIM_URL, {
            'case_id': case_id,
            'client_id': client_id,
            'email': email,
        }, format='json', HTTP_AUTHORIZATION=f'Bearer {HEALTH_HACK_KEY}')

    def test_first_claim_wins_ticket(self):
        self._seed_guess()
        resp = self._claim(email='Winner@Example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {
            'result': 'ticket',
            'prize_kind': 'free_ticket',
            'redemption_url': 'https://luma.test/free',
            'already_claimed': False,
        })

        guess = SimDiagnosisGuess.objects.get()
        self.assertEqual(guess.outcome, 'ticket')
        self.assertEqual(guess.email, 'winner@example.com')  # normalized lower
        self.assertIsNotNone(guess.claimed_at)
        winner = SimCaseWinner.objects.get()
        self.assertEqual(winner.case_id, 1)
        self.assertEqual(winner.guess, guess)

    def test_second_claimant_gets_discount(self):
        self._seed_guess(client_id=CLIENT_A)
        self._seed_guess(client_id=CLIENT_B)
        self._claim(client_id=CLIENT_A, email='first@example.com')

        resp = self._claim(client_id=CLIENT_B, email='second@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['result'], 'discount')
        self.assertEqual(resp.data['redemption_url'], 'https://luma.test/discount')
        self.assertEqual(SimCaseWinner.objects.count(), 1)
        self.assertEqual(
            SimDiagnosisGuess.objects.get(client_id=CLIENT_B).outcome, 'discount')

    def test_claim_is_idempotent(self):
        self._seed_guess()
        first = self._claim()
        self.assertEqual(first.data['result'], 'ticket')

        again = self._claim(email='other@example.com')  # even with a new email
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data['result'], 'ticket')
        self.assertTrue(again.data['already_claimed'])
        self.assertEqual(again.data['redemption_url'], 'https://luma.test/free')
        # Stored email unchanged, still exactly one winner row.
        self.assertEqual(SimDiagnosisGuess.objects.get().email, 'doc@example.com')
        self.assertEqual(SimCaseWinner.objects.count(), 1)

    def test_claim_without_correct_guess_404s(self):
        # No guess at all
        resp = self._claim()
        self.assertEqual(resp.status_code, 404)
        # Incorrect guess has nothing to claim
        self._seed_guess(is_correct=False)
        resp = self._claim()
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(SimCaseWinner.objects.count(), 0)

    def test_duplicate_email_on_same_case_409s(self):
        self._seed_guess(client_id=CLIENT_A)
        self._seed_guess(client_id=CLIENT_B)
        self._claim(client_id=CLIENT_A, email='same@example.com')

        resp = self._claim(client_id=CLIENT_B, email='Same@Example.com')
        self.assertEqual(resp.status_code, 409)
        # B's guess stays claimable with a different email.
        b = SimDiagnosisGuess.objects.get(client_id=CLIENT_B)
        self.assertEqual(b.outcome, 'pending_claim')
        resp = self._claim(client_id=CLIENT_B, email='different@example.com')
        self.assertEqual(resp.data['result'], 'discount')

    def test_same_email_on_other_case_is_fine(self):
        self._seed_guess(case_id=1, client_id=CLIENT_A)
        self._seed_guess(case_id=2, client_id=CLIENT_B)
        self._claim(case_id=1, client_id=CLIENT_A, email='doc@example.com')
        with self.settings(HEALTH_HACK_ACTIVE_CASE_ID=2):
            resp = self._claim(case_id=2, client_id=CLIENT_B, email='doc@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['result'], 'ticket')  # fresh case, fresh slot

    def test_claim_rejects_non_open_case(self):
        self._seed_guess(case_id=9)
        resp = self._claim(case_id=9)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'inactive_case')
        self.assertEqual(
            SimDiagnosisGuess.objects.get(case_id=9).outcome,
            SimDiagnosisGuess.OUTCOME_PENDING_CLAIM,
        )

    def test_invalid_email_400s(self):
        self._seed_guess()
        resp = self._claim(email='not-an-email')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(SimDiagnosisGuess.objects.get().outcome, 'pending_claim')

    def test_prize_assignment_does_not_depend_on_claim_order(self):
        self._seed_guess(client_id=CLIENT_A)
        self._seed_guess(client_id=CLIENT_B)
        # B claims first but was assigned the consolation prize at guess time.
        resp = self._claim(client_id=CLIENT_B, email='raced@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['result'], 'discount')

    @override_settings(HEALTH_HACK_ACTIVE_CASE_ID=7)
    def test_full_flow_through_record_endpoint(self):
        """record → claim wired end to end through both endpoints."""
        service = APIClient()
        for client_id, correct in ((CLIENT_A, True), (CLIENT_B, True), (CLIENT_C, False)):
            resp = service.post(RECORD_URL, {
                'case_id': 7,
                'case_title': 'Pressure Behind the Curtain',
                'client_id': client_id,
                'guess_text': 'cerebral venous thrombosis' if correct else 'migraine',
                'is_correct': correct,
            }, format='json', HTTP_X_API_KEY=ROO_KEY)
            self.assertEqual(resp.status_code, 200)

        self.assertEqual(self._claim(case_id=7, client_id=CLIENT_A,
                                     email='a@example.com').data['result'], 'ticket')
        self.assertEqual(self._claim(case_id=7, client_id=CLIENT_B,
                                     email='b@example.com').data['result'], 'discount')
        self.assertEqual(self._claim(case_id=7, client_id=CLIENT_C,
                                     email='c@example.com').status_code, 404)
        winner = SimDiagnosisGuess.objects.get(case_id=7, client_id=CLIENT_A)
        self.assertEqual(winner.email, 'a@example.com')
        self.assertEqual(winner.case_title, 'Pressure Behind the Curtain')
        self.assertTrue(winner.is_correct)
        self.assertEqual(winner.guess_text, 'cerebral venous thrombosis')


@override_settings(
    ROO_API_KEY=ROO_KEY,
    HEALTH_HACK_API_KEY=HEALTH_HACK_KEY,
    HEALTH_HACK_ACTIVE_CASE_ID=1,
    HEALTH_HACK_FREE_TICKET_URL='https://luma.test/free',
    HEALTH_HACK_DISCOUNT_URL='https://luma.test/discount',
    HEALTH_HACK_FREE_TICKET_URLS={},
)
class SimGuessStatusTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _status(self, client_id=CLIENT_A, key=HEALTH_HACK_KEY):
        headers = {}
        if key is not None:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {key}'
        return self.client.get(
            STATUS_URL,
            {'client_id': client_id},
            **headers,
        )

    def test_status_requires_worker_key(self):
        self.assertEqual(self._status(key=None).status_code, 403)

    def test_status_transitions_from_eligible_to_claim_to_completed(self):
        self.assertEqual(self._status().data['state'], 'eligible')
        self.assertFalse(SimParticipant.objects.filter(id=CLIENT_A).exists())

        guess = SimDiagnosisGuess.objects.create(
            case_id=1,
            client_id=CLIENT_A,
            guess_text='adrenal crisis',
            is_correct=True,
            outcome='pending_claim',
            prize_kind='free_ticket',
        )
        SimCaseWinner.objects.create(case_id=1, guess=guess)
        pending = self._status().data
        self.assertEqual(pending['state'], 'awaiting_claim')
        self.assertEqual(pending['prize_kind'], 'free_ticket')
        self.assertIsNone(pending['redemption_url'])

        guess.outcome = 'ticket'
        guess.save(update_fields=['outcome'])
        completed = self._status().data
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['redemption_url'], 'https://luma.test/free')

    def test_incorrect_guess_is_locked(self):
        SimDiagnosisGuess.objects.create(
            case_id=1,
            client_id=CLIENT_A,
            guess_text='gastro',
            is_correct=False,
            outcome='incorrect',
            prize_kind='none',
        )
        self.assertEqual(self._status().data['state'], 'locked')


@override_settings(
    ROO_API_KEY=ROO_KEY,
    HEALTH_HACK_API_KEY=HEALTH_HACK_KEY,
    HEALTH_HACK_ACTIVE_CASE_ID=1,
    HEALTH_HACK_OPEN_CASE_IDS=[1, 2],
    HEALTH_HACK_FREE_TICKET_URL='https://luma.test/free',
    HEALTH_HACK_DISCOUNT_URL='https://luma.test/discount',
    HEALTH_HACK_FREE_TICKET_URLS={1: 'https://luma.test/sash', 2: 'https://luma.test/leila'},
)
class SimGuessTwoCaseContestTests(TestCase):
    """Two-patient ward: concurrent one-guess books with per-case coupons."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _record(self, *, case_id, client_id=CLIENT_A, correct=True):
        return self.client.post(RECORD_URL, {
            'case_id': case_id,
            'case_title': f'Case {case_id}',
            'client_id': client_id,
            'guess_text': 'adrenal crisis' if correct else 'gastro',
            'is_correct': correct,
        }, format='json', HTTP_X_API_KEY=ROO_KEY)

    def _claim(self, *, case_id, client_id=CLIENT_A, email='doc@example.com'):
        return self.client.post(CLAIM_URL, {
            'case_id': case_id,
            'client_id': client_id,
            'email': email,
        }, format='json', HTTP_AUTHORIZATION=f'Bearer {HEALTH_HACK_KEY}')

    def _status(self, client_id=CLIENT_A):
        return self.client.get(
            STATUS_URL,
            {'client_id': client_id},
            HTTP_AUTHORIZATION=f'Bearer {HEALTH_HACK_KEY}',
        )

    def test_each_open_case_has_its_own_winner_slot_and_coupon(self):
        # The same player solves both cases first: two tickets, two coupons.
        self.assertEqual(self._record(case_id=1).data['prize_kind'], 'free_ticket')
        self.assertEqual(self._record(case_id=2).data['prize_kind'], 'free_ticket')
        self.assertEqual(
            self._claim(case_id=1, email='a@example.com').data['redemption_url'],
            'https://luma.test/sash',
        )
        self.assertEqual(
            self._claim(case_id=2, email='a@example.com').data['redemption_url'],
            'https://luma.test/leila',
        )

    def test_runner_up_gets_the_shared_discount(self):
        self._record(case_id=2, client_id=CLIENT_A)
        resp = self._record(case_id=2, client_id=CLIENT_B)
        self.assertEqual(resp.data['prize_kind'], 'discount_30')
        claim = self._claim(case_id=2, client_id=CLIENT_B, email='b@example.com')
        self.assertEqual(claim.data['redemption_url'], 'https://luma.test/discount')

    def test_burning_one_book_leaves_the_other_open(self):
        self._record(case_id=2, correct=False)
        resp = self._status()
        by_case = {case['case_id']: case for case in resp.data['cases']}
        self.assertEqual(set(by_case), {1, 2})
        self.assertEqual(by_case[1]['state'], 'eligible')
        self.assertEqual(by_case[2]['state'], 'locked')
        # Legacy top-level fields keep reporting the active case for a
        # not-yet-updated Worker.
        self.assertEqual(resp.data['case_id'], 1)
        self.assertEqual(resp.data['state'], 'eligible')

    def test_completed_case_reports_its_own_coupon_in_status(self):
        self._record(case_id=2)
        self._claim(case_id=2, email='w@example.com')
        resp = self._status()
        by_case = {case['case_id']: case for case in resp.data['cases']}
        self.assertEqual(by_case[2]['state'], 'completed')
        self.assertEqual(by_case[2]['redemption_url'], 'https://luma.test/leila')
        self.assertEqual(by_case[1]['state'], 'eligible')
