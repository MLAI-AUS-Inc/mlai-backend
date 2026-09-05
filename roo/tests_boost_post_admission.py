from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import User
from roo.models import BoostPostAdmission, Ledger, PointsAccount
from roo.services import PointsService


@override_settings(ROO_API_KEY='test-roo-key')
class BoostPostAdmissionAPITests(APITestCase):
    def setUp(self):
        self.url = reverse('boost-post-admission')
        self.user = User.objects.create_user(
            email='boost-founder@example.com',
            slack_id='UFOUNDER123',
        )
        PointsService.award(
            user=self.user,
            delta=20,
            source='MANUAL',
            description='Boost admission test balance',
            created_by_slack_id='UADMIN123',
            idempotency_key='boost_admission_test_balance',
        )
        self.client.credentials(HTTP_X_API_KEY='test-roo-key')
        self.payload = {
            'submission_key': 'boost-post:TTEAM123:CBOOST123:1796170000.000100',
            'workspace_id': 'TTEAM123',
            'channel_id': 'CBOOST123',
            'root_message_ts': '1796170000.000100',
            'poster_slack_id': 'UFOUNDER123',
            'root_text': 'Please boost https://www.linkedin.com/posts/example-123',
            'social_post_url': 'https://www.linkedin.com/posts/example-123',
        }

    def test_requires_strict_roo_api_key(self):
        self.client.credentials()
        response = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_standard_post_costs_eight_points_and_is_idempotent(self):
        first = self.client.post(self.url, self.payload, format='json')
        second = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data['status'], 'approved')
        self.assertEqual(first.data['base_cost_points'], 8)
        self.assertEqual(first.data['charged_points'], 8)
        self.assertFalse(first.data['discount_applied'])
        self.assertEqual(first.data['new_balance'], 12)
        self.assertFalse(first.data['idempotent_replay'])
        self.assertTrue(second.data['idempotent_replay'])
        self.assertEqual(
            Ledger.objects.filter(reference_type='BOOST_POST').count(),
            1,
        )
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 12)

    def test_ready_verified_australian_startup_gets_half_price(self):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        organization = Organization.objects.create(
            name='Boost Pty Ltd',
            domain='boost.example',
        )
        UserStartupBinding.objects.create(user=self.user, organization=organization)
        profile = VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organization,
            name='Boost Pty Ltd',
            registered=True,
            abn='89000000019',
            acn='000000019',
            abr_verified_at=timezone.now(),
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=timezone.localdate().replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['charged_points'], 4)
        self.assertTrue(response.data['discount_applied'])
        self.assertEqual(response.data['new_balance'], 16)

    def test_insufficient_points_rejects_without_ledger_debit(self):
        account = PointsAccount.objects.get(user=self.user)
        account.balance = 7
        account.earned_balance = 7
        account.save(update_fields=['balance', 'earned_balance'])

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(response.data['code'], 'insufficient_points')
        self.assertEqual(response.data['charged_points'], 8)
        self.assertEqual(response.data['new_balance'], 7)
        self.assertFalse(Ledger.objects.filter(reference_type='BOOST_POST').exists())
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 7)

    def test_insufficient_admission_can_be_atomically_rechecked_after_earning_points(self):
        account = PointsAccount.objects.get(user=self.user)
        account.balance = 7
        account.earned_balance = 7
        account.save(update_fields=['balance', 'earned_balance'])
        first = self.client.post(self.url, self.payload, format='json')

        account.balance = 12
        account.earned_balance = 12
        account.save(update_fields=['balance', 'earned_balance'])
        recheck_payload = {**self.payload, 'recheck_insufficient_points': True}
        second = self.client.post(self.url, recheck_payload, format='json')
        replay = self.client.post(self.url, recheck_payload, format='json')

        self.assertEqual(first.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.data['status'], 'approved')
        self.assertTrue(second.data['recheck_requested'])
        self.assertEqual(second.data['new_balance'], 4)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(
            Ledger.objects.filter(reference_type='BOOST_POST').count(),
            1,
        )

    def test_insufficient_admission_stays_terminal_without_explicit_recheck(self):
        account = PointsAccount.objects.get(user=self.user)
        account.balance = 7
        account.earned_balance = 7
        account.save(update_fields=['balance', 'earned_balance'])
        first = self.client.post(self.url, self.payload, format='json')

        account.balance = 12
        account.earned_balance = 12
        account.save(update_fields=['balance', 'earned_balance'])
        replay = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(first.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(replay.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertFalse(Ledger.objects.filter(reference_type='BOOST_POST').exists())

    def test_recheck_flag_must_be_boolean(self):
        response = self.client.post(
            self.url,
            {**self.payload, 'recheck_insufficient_points': 'true'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['code'], 'invalid_post')

    def test_unlinked_member_is_rejected(self):
        self.payload['poster_slack_id'] = 'UUNKNOWN123'

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['code'], 'member_unlinked')
        self.assertEqual(BoostPostAdmission.objects.get().status, 'member_unlinked')

    def test_any_website_link_is_eligible(self):
        self.payload['social_post_url'] = 'https://example.com/not-a-social-post'

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'approved')
        self.assertEqual(response.data['charged_points'], 8)
        self.assertEqual(
            BoostPostAdmission.objects.get().social_post_url,
            'https://example.com/not-a-social-post',
        )

    def test_post_without_a_link_is_still_eligible(self):
        self.payload.pop('social_post_url')
        self.payload['root_text'] = 'Please help boost my startup update'

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'approved')
        self.assertEqual(response.data['charged_points'], 8)
        self.assertEqual(BoostPostAdmission.objects.get().social_post_url, '')

    def test_idempotency_key_cannot_be_rebound_to_another_member(self):
        first = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.payload['poster_slack_id'] = 'UOTHER123'

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'idempotency_conflict')
        self.assertEqual(Ledger.objects.filter(reference_type='BOOST_POST').count(), 1)
