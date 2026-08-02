import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from community_chat.models import CommunityChatBootstrapToken, CommunityChatDevice


User = get_user_model()


class CommunityChatDeviceMetadataTests(TestCase):
    def test_device_records_installation_and_platform_metadata(self):
        user = User.objects.create_user(email="device@example.com")
        installation_id = uuid.uuid4()

        device = CommunityChatDevice.objects.create(
            user=user,
            public_key="a" * 64,
            installation_id=installation_id,
            client_id="mlai-chat-ios",
            platform="ios",
            name="Sam's iPhone",
        )

        self.assertEqual(device.installation_id, installation_id)
        self.assertEqual(device.client_id, "mlai-chat-ios")
        self.assertEqual(device.platform, "ios")
        self.assertEqual(device.name, "Sam's iPhone")

    def test_bootstrap_token_is_bound_to_installation_and_client(self):
        user = User.objects.create_user(email="bootstrap@example.com")
        installation_id = uuid.uuid4()

        token = CommunityChatBootstrapToken.objects.create(
            user=user,
            public_key="b" * 64,
            installation_id=installation_id,
            client_id="mlai-chat-android",
            token_hash="c" * 64,
            expires_at="2099-01-01T00:00:00Z",
        )

        self.assertEqual(token.installation_id, installation_id)
        self.assertEqual(token.client_id, "mlai-chat-android")
