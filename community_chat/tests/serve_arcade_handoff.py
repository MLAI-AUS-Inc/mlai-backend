"""Serve the real Arcade API with one synthetic member and two native sessions."""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from wsgiref.simple_server import make_server


def main(fixture_path):
    os.environ["DJANGO_SETTINGS_MODULE"] = "mlai.settings"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

    import django
    from django.conf import settings

    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    }
    settings.CACHES = {
        alias: {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": f"arcade-handoff-{alias}",
        }
        for alias in settings.CACHES
    }
    settings.COMMUNITY_CHAT_RELAY_URL = "wss://chat.mlai.au"
    settings.COMMUNITY_CHAT_VOLUNTEER_COMMUNITY = "arcade-handoff-test"
    settings.COMMUNITY_CHAT_VOLUNTEER_ENABLED = True
    settings.COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED = True
    settings.COMMUNITY_CHAT_SIGNUP_ENABLED = False
    settings.MIDDLEWARE = []
    settings.ALLOWED_HOSTS = ["127.0.0.1"]
    django.setup()

    from django.contrib.auth import get_user_model
    from django.core.wsgi import get_wsgi_application
    from django.db import connection

    from community_chat import arcade
    from community_chat.account_sessions import issue_account_session
    from community_chat.models import (
        AccountDeletionRequest,
        CommunityChatAccountSession,
        CommunityChatDevice,
        CommunityChatEmailChange,
        CommunityChatEmailChangeJob,
    )
    from community_chat.onboarding_models import CommunityMemberProfile
    from community_chat.roo_avatar.models import CommunityRooAvatar
    from community_chat.volunteer.access import community_id
    from community_chat.volunteer.models import (
        VolunteerMemberState,
        VolunteerOpeningEntry,
        VolunteerOpportunity,
        VolunteerProject,
        VolunteerRecognition,
        VolunteerSourceReceipt,
    )
    from core.models import AccountBan, UserEmailAlias
    from roo.models import Ledger, PointsAccount, Task

    User = get_user_model()
    # Only materialize the current models needed by the fixture in a throwaway
    # in-memory database. No migration runner or existing database is involved.
    with connection.schema_editor() as schema:
        for model in (
            User, UserEmailAlias, AccountBan, AccountDeletionRequest, CommunityChatAccountSession,
            CommunityChatDevice, CommunityChatEmailChange, CommunityChatEmailChangeJob,
            CommunityMemberProfile, CommunityRooAvatar, Task, Ledger, PointsAccount,
            VolunteerProject, VolunteerOpportunity, VolunteerMemberState,
            VolunteerOpeningEntry, VolunteerRecognition, VolunteerSourceReceipt,
        ):
            schema.create_model(model)

    member = User.objects.create_user(email="arcade-handoff@example.test")
    VolunteerMemberState.objects.create(
        community=community_id(), user=member, historical_microroo=106_000_000,
    )
    arcade.earn_level_rounds(member, 106_000_000, 2_000_000, action="helpful_answer")
    round_id = str(arcade.rounds_for(member).get().pk)

    def credentials(device):
        session = issue_account_session(member, SimpleNamespace(
            public_key=("1" if device == "desktop" else "2") * 64,
            installation_id=uuid4(),
            client_id="mlai-chat",
            origin="mlaichat://callback",
            platform=device,
            device_name=device,
        ))
        return {"accessToken": session.access_token, "refreshToken": session.refresh_token}

    with make_server("127.0.0.1", 0, get_wsgi_application()) as server, patch.object(
        arcade, "draw_prize", return_value=("roo_points", 5),
    ) as draw:
        def app(environ, start_response):
            # Expose only the Arcade API and fixture assertions to local tests.
            if environ["PATH_INFO"] == "/fixture/state":
                row = arcade.rounds_for(member).get()
                state = {
                    "roundId": str(row.pk),
                    "status": row.status,
                    "draws": draw.call_count,
                    "creditCount": Ledger.objects.filter(
                        user=member, reference_type="MLAI_ARCADE", reference_id=round_id,
                    ).count(),
                    "balance": PointsAccount.objects.filter(user=member)
                    .values_list("balance", flat=True).first() or 0,
                }
                start_response("200 OK", [("Content-Type", "application/json")])
                return [json.dumps(state).encode()]
            if not environ["PATH_INFO"].startswith("/api/v1/community-chat/arcade/"):
                start_response("404 Not Found", [])
                return [b""]
            return server_app(environ, start_response)

        server_app = server.get_app()
        server.set_app(app)
        session = {
            "apiRoot": f"http://127.0.0.1:{server.server_port}/api/v1",
            "roundId": round_id,
            "desktop": credentials("desktop"),
            "mobile": credentials("mobile"),
        }
        with open(fixture_path, "x", encoding="utf-8", opener=_private_file) as fixture:
            json.dump(session, fixture)
        server.serve_forever()


def _private_file(path, flags):
    return os.open(path, flags, 0o600)


if __name__ == "__main__":
    main(sys.argv[1])
