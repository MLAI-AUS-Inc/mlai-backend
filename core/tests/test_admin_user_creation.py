from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.forms import CustomUserCreationForm


User = get_user_model()


class CustomUserCreationFormTests(TestCase):
    def test_accepts_email_password1_and_password2(self):
        form = CustomUserCreationForm(
            data={
                'email': 'new-admin-user@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('password1', form.fields)
        self.assertIn('password2', form.fields)
        self.assertNotIn('password', form.fields)
        self.assertNotIn('password_2', form.fields)

    def test_password_mismatch_returns_validation_error(self):
        form = CustomUserCreationForm(
            data={
                'email': 'mismatch@example.com',
                'password1': 'StrongPass123!',
                'password2': 'DifferentPass123!',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('password2', form.errors)
        self.assertIn('match', str(form.errors['password2']).lower())


class UserAdminAddViewTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            email='admin@example.com',
            password='AdminPass123!',
        )
        self.client.force_login(self.admin_user)

    def test_admin_add_user_creates_user_with_usable_password(self):
        response = self.client.post(
            reverse('admin:core_user_add'),
            {
                'email': 'created-through-admin@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
                '_save': 'Save',
            },
        )

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email='created-through-admin@example.com')
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password('StrongPass123!'))
        self.assertEqual(user.role, 'participant')
