from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from esafety.models import Team as EsafetyTeam

User = get_user_model()

class UserPermissionTests(APITestCase):
    def setUp(self):
        # Create users
        self.owner = User.objects.create_user(email='owner@example.com', password='password', role='participant')
        self.teammate = User.objects.create_user(email='teammate@example.com', password='password', role='participant')
        self.stranger = User.objects.create_user(email='stranger@example.com', password='password', role='participant')
        self.superuser = User.objects.create_superuser(email='admin@example.com', password='password')

        # Create team and assign owner and teammate
        self.team = EsafetyTeam.objects.create(team_name='Test Team')
        self.team.members.add(self.owner)
        self.team.members.add(self.teammate)

        # URLs
        self.owner_url = reverse('user_detail', kwargs={'pk': self.owner.pk})
        self.teammate_url = reverse('user_detail', kwargs={'pk': self.teammate.pk})
        self.stranger_url = reverse('user_detail', kwargs={'pk': self.stranger.pk})

    def test_owner_can_read_own_profile(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self.owner_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_update_own_profile(self):
        self.client.force_authenticate(user=self.owner)
        data = {'first_name': 'New Name'}
        response = self.client.patch(self.owner_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, 'New Name')

    def test_teammate_can_read_profile(self):
        self.client.force_authenticate(user=self.teammate)
        response = self.client.get(self.owner_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_teammate_cannot_update_profile(self):
        self.client.force_authenticate(user=self.teammate)
        data = {'first_name': 'Hacked'}
        response = self.client.patch(self.owner_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_stranger_cannot_read_profile(self):
        self.client.force_authenticate(user=self.stranger)
        response = self.client.get(self.owner_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_stranger_cannot_update_profile(self):
        self.client.force_authenticate(user=self.stranger)
        data = {'first_name': 'Hacked'}
        response = self.client.patch(self.owner_url, data)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_superuser_can_read_any_profile(self):
        self.client.force_authenticate(user=self.superuser)
        response = self.client.get(self.owner_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_superuser_can_update_any_profile(self):
        self.client.force_authenticate(user=self.superuser)
        data = {'first_name': 'Admin Edit'}
        response = self.client.patch(self.owner_url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, 'Admin Edit')
