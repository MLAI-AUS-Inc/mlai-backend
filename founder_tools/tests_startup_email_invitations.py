from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.firebase_utils import FIREBASE_STORAGE_BUCKET
from core.models import User, UserEmailAlias
from organizations.models import Organization
from system_email.models import SystemEmailSuppression
from system_email.transport_resend import SystemEmailRetryable
from system_email.webhooks import record_event

from . import membership, startup_email_invitations as invites, startup_invite_delivery as worker
from . import startup_team
from .models import StartupMembership, StartupTeamInvitation, StartupTeamInviteDelivery, VibeRaisingCompany, VibeRaisingProfile

SETTINGS = dict(
    MY_STARTUP_EMAIL_INVITES_ENABLED=True,
    MY_STARTUP_SHARED_ACCESS_ENABLED=True,
    MY_STARTUP_TEAM_ENABLED=True,
    COMMUNITY_CHAT_FRONTEND_URL="https://chat.synthetic.test",
    SYSTEM_EMAIL_RESEND_API_KEY="fixture-key",
    MLAI_SYSTEM_FROM_EMAIL="test@example.test",
)
BASE = "/api/v1/my-startup/team/"


def person(name, *, verified=True):
    return User.objects.create_user(f"{name}@example.test", first_name=name,
                                    email_verified_at=timezone.now() if verified else None)


def startup(name, creator, *, shared=True, cutover=True):
    organization = Organization.objects.create(name=name, domain=f"{name.lower()}.example.test")
    profile, _ = VibeRaisingProfile.objects.get_or_create(user=creator, defaults={"role": "founder"})
    company = VibeRaisingCompany.objects.create(name=name, organization=organization, profile=profile)
    if shared:
        with override_settings(MY_STARTUP_EMAIL_INVITES_ENABLED=cutover):
            membership.activate_new_company(company, creator)
    if cutover and not shared:
        call_command("cutover_startup_email_invites", company=str(company.pk), apply=True, stdout=StringIO())
    company.refresh_from_db()
    return company


@override_settings(**SETTINGS)
class StartupEmailInvitationTests(TestCase):
    def setUp(self):
        self.alice = person("Alice")
        self.company = startup("Atlas", self.alice)
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def issue(self, email="new@example.test"):
        result = invites.send_invitation(self.company, self.alice, email=email)
        return StartupTeamInvitation.objects.get(pk=result["invitation"]["invitationId"])

    def test_exact_recipient_and_startup_with_signup_after_send(self):
        second = startup("Beacon", self.alice)
        email = " New@Example.Test "
        response = self.client.post(f"{BASE}invitations/?company_id={self.company.pk}", {"email": email})
        self.assertEqual(response.status_code, 202, response.data)
        invitation = StartupTeamInvitation.objects.get()
        self.assertEqual(invitation.email, "new@example.test")
        team = self.client.get(f"{BASE}?company_id={self.company.pk}").data
        self.assertEqual(team["invitationMode"], "email")
        self.assertNotIn("invite", team)
        self.assertNotIn(str(invitation.public_id), str(response.data))
        self.assertNotIn(str(invitation.public_id), str(team))
        self.assertEqual(invites.send_invitation(self.company, self.alice, email=email)["status"], "existing")
        self.assertEqual(invites.send_invitation(second, self.alice, email=email)["status"], "queued")
        url = f"{BASE}invitations/{invitation.public_id}/accept/"
        self.client.force_authenticate(person("someone-else"))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        recipient = person("new", verified=False)
        self.client.force_authenticate(recipient)
        self.assertEqual(self.client.post(url).status_code, 404)
        recipient.email_verified_at = timezone.now()
        recipient.save(update_fields=["email_verified_at"])
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["startupName"], "Atlas")
        self.assertFalse(membership.has_membership(recipient, self.company))
        for _ in range(2):
            self.assertEqual(self.client.post(url).data["companyId"], str(self.company.pk))
        self.assertTrue(membership.has_membership(recipient, self.company))
        self.assertFalse(membership.has_membership(recipient, second))
        self.assertEqual(self.company.membership_events.filter(kind="joined", subject=recipient).count(), 1)
        self.assertEqual(invites.send_invitation(self.company, recipient, email="other@example.test")["status"], "queued")

    def test_resend_retry_and_revocation(self):
        recipient = person("new")
        invitation = self.issue()
        old = invitation.public_id
        with patch("system_email.transport_resend.send", return_value="first-message"):
            worker.process_due()
        delivery = invitation.deliveries.get()
        delivery.created_at -= invites.COOLDOWN
        delivery.save(update_fields=["created_at"])
        action = uuid4()
        command = dict(invitation_id=invitation.pk, action_id=str(action))
        self.assertEqual(invites.send_invitation(self.company, self.alice, **command)["status"], "queued")
        self.assertEqual(invites.send_invitation(self.company, self.alice, **command)["status"], "existing")
        invitation.refresh_from_db()
        with self.assertRaises(startup_team.TeamError):
            invites.accept(recipient, old, confirm=True)
        invites.cancel(self.company, self.alice, invitation.pk)
        with self.assertRaises(startup_team.TeamError):
            invites.accept(recipient, invitation.public_id, confirm=True)
        with patch("system_email.transport_resend.send") as transport:
            self.assertEqual(worker.process_due(), 1)
        transport.assert_not_called()

    def test_verified_alias_and_removed_inviter(self):
        recipient = person("Jo")
        alias = UserEmailAlias.objects.create(user=recipient, email="alias@example.test")
        invitation = self.issue(alias.email)
        with self.assertRaises(startup_team.TeamError):
            invites.accept(recipient, invitation.public_id, confirm=True)
        alias.verified_at = timezone.now()
        alias.save(update_fields=["verified_at"])
        self.assertEqual(invites.accept(recipient, invitation.public_id, confirm=True)["status"], "joined")
        other = person("Other")
        issued = invites.send_invitation(self.company, recipient, email=other.email)
        issued = StartupTeamInvitation.objects.get(pk=issued["invitation"]["invitationId"])
        startup_team.update_member(organization=self.company.organization, user=self.alice,
                                   member_id=StartupMembership.objects.get(company=self.company, user=recipient).pk,
                                   remove=True)
        with startup_team._locked(self.company.organization):
            membership.admit(self.company, recipient, source="request", actor=self.alice)
        with self.assertRaises(startup_team.TeamError):
            invites.accept(other, issued.public_id, confirm=True)

    def test_cutover_retires_links_independently_of_send_switch(self):
        company = startup("Beacon", self.alice, cutover=False)
        token = urlsplit(startup_team.invite_link(organization=company.organization,
                                                  user=self.alice, action="create")["url"]).fragment
        call_command("cutover_startup_email_invites", company=str(company.pk), apply=True, stdout=StringIO())
        with self.settings(MY_STARTUP_EMAIL_INVITES_ENABLED=False):
            with self.assertRaises(startup_team.TeamError):
                startup_team.join_invite(user=person("Jo"), token=token)
            with self.assertRaises(startup_team.TeamError):
                startup_team.invite_link(organization=company.organization, user=self.alice, action="create")

    def test_legacy_email_admission_can_be_adopted(self):
        company = startup("Beacon", self.alice, shared=False)
        recipient = person("Jo")
        result = invites.send_invitation(company, self.alice, email=recipient.email)
        invitation = StartupTeamInvitation.objects.get(pk=result["invitation"]["invitationId"])
        self.assertEqual(invites.accept(recipient, invitation.public_id)["accessPolicy"], "legacy")
        self.assertEqual(invites.accept(recipient, invitation.public_id, confirm=True)["status"], "joined")
        self.assertTrue(startup_team.member_companies(recipient))
        pending = invites.send_invitation(company, self.alice, email="extra@example.test")
        call_command("migrate_startup_membership", company=str(company.pk), apply=True, stdout=StringIO())
        self.assertTrue(membership.has_membership(recipient, company))
        self.assertIsNotNone(StartupTeamInvitation.objects.get(pk=pending["invitation"]["invitationId"]).revoked_at)

    def test_rendered_mail_retries_with_same_payload_and_feedback(self):
        logo = (f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_STORAGE_BUCKET}/o/"
                f"company-avatars%2F{self.company.pk}%2Fapproved.png?alt=media&token=fixture")
        self.company.avatar_url = logo
        self.company.save(update_fields=["avatar_url"])
        invitation = self.issue()
        with patch("system_email.transport_resend.send",
                   side_effect=[SystemEmailRetryable("resend_429"), "mock-message"]) as transport:
            self.assertEqual(worker.process_due(), 1)
            delivery = invitation.deliveries.get()
            self.assertEqual(delivery.state, delivery.State.QUEUED)
            self.company.name = "Changed"
            self.company.save(update_fields=["name"])
            invitation.deliveries.update(available_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(worker.process_due(), 1)
        first, second = transport.call_args_list
        self.assertEqual(first, second)
        body = first.args[0]
        self.assertEqual(body["to"], [invitation.email])
        self.assertIn('Accept invitation', body["html"])
        self.assertIn("approved.png?alt=media&amp;token=fixture", body["html"])
        self.assertIn("mlai-roo.png", body["html"])
        self.assertIn("full access", body["text"])
        self.assertIn(str(invitation.public_id), body["text"])
        self.assertEqual(invites.pending(self.company)[0]["deliveryStatus"], "sent")
        event = {"type": "email.bounced", "email_id": "mock-message", "recipient": invitation.email,
                 "bounce_type": "permanent", "at": timezone.now()}
        self.assertEqual(record_event(event, "startup-invite-bounce")["status"], "recorded")
        self.assertEqual(record_event(event, "startup-invite-bounce")["status"], "duplicate")
        self.assertEqual(invites.pending(self.company)[0]["deliveryStatus"], "failed")
        self.assertTrue(SystemEmailSuppression.objects.filter(email=invitation.email).exists())

    def test_suppression_never_reaches_transport(self):
        invitation = self.issue()
        SystemEmailSuppression.objects.create(email=invitation.email, reason="hard_bounce")
        with patch("system_email.transport_resend.send") as transport:
            worker.process_due()
        transport.assert_not_called()
        self.assertEqual(invites.pending(self.company)[0]["deliveryStatus"], "failed")

    def test_delivery_recovers_after_provider_success_before_finalizing(self):
        invitation = self.issue()
        now = timezone.now()
        row = worker.claim(now)
        with patch("system_email.transport_resend.send", return_value="accepted") as transport:
            with patch.object(worker, "_finish", side_effect=RuntimeError("worker interrupted")):
                with self.assertRaises(RuntimeError):
                    worker.deliver(row, now)
            recovered = worker.claim(now + worker.LEASE + timedelta(seconds=1))
            worker.deliver(recovered, now + worker.LEASE + timedelta(seconds=1))
        transport.assert_called_once()
        self.assertEqual(invitation.deliveries.get().state, StartupTeamInviteDelivery.State.SENT)


@override_settings(**SETTINGS)
@skipUnless(connection.vendor == "postgresql", "Row lock regression requires PostgreSQL.")
class ConcurrentInvitationTests(TransactionTestCase):
    def test_two_members_create_one_delivery_and_accept_once(self):
        alice, ben, jo = person("Alice"), person("Ben"), person("Jo")
        company = startup("Atlas", alice)
        with startup_team._locked(company.organization):
            membership.admit(company, ben, source="request", actor=alice)

        def send(actor):
            try:
                return invites.send_invitation(company, actor, email=jo.email)["status"]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(send, (alice, ben))), ["queued", "existing"])
        row = StartupTeamInvitation.objects.get()
        self.assertEqual(row.deliveries.count(), 1)

        def accept(_):
            try:
                return invites.accept(jo, row.public_id, confirm=True)["status"]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(accept, range(2))), ["joined", "joined"])
        self.assertEqual(company.membership_events.filter(subject=jo, kind="joined").count(), 1)
