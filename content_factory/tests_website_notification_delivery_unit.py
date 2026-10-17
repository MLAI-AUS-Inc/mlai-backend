"""Actual notice/outbox/transport logic with only persistence and providers replaced."""
import base64
from contextlib import ExitStack,nullcontext
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock,Mock,patch
from uuid import uuid4
from django.test import SimpleTestCase,override_settings
from django.utils import timezone
from agent_runtime import native_crypto
from agent_runtime.webhook_transport import WebhookResponse,CallbackEndpointError
from . import website_notification_outbox as outbox,website_notification_transport as transport,website_prepare as prepare,tests_website_goal_unit as fixtures
from .website_models import WebsiteConnectionOperation

KEY="01"*32 # Synthetic local signing fixture, never a deployed key.
RECIPIENT=native_crypto.public_key(native_crypto.signing_key("02"*32))


def rows(first=None,values=()):
    result=MagicMock()
    for method in ("filter","select_for_update","select_related","order_by","values_list","distinct"):
        getattr(result,method).return_value=result
    result.first.return_value=first;result.exists.return_value=True;result.update.return_value=1
    result.__getitem__.return_value=list(values)
    return result


@override_settings(ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY=KEY,COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",RESEND_API_KEY="synthetic",RESEND_FROM_EMAIL="MLAI <notifications@example.test>")
class TransportTests(SimpleTestCase):
    def test_actual_signed_event_http_auth_and_replay_keep_same_message_id(self):
        notice={"id":"a"*64,"state":"needs_you","text":"Review setup","link":"https://chat.mlai.au/my-startup/create?company_id=9"}
        event=transport.message_event(str(uuid4()),[RECIPIENT],notice,1234567)
        captured=[]
        def send(url,body,headers,**options):
            captured.append((url,body,headers,options))
            return WebhookResponse(200,json.dumps({"accepted":True,"event_id":json.loads(body)["id"],"message":"stored"}).encode())
        with patch.object(transport,"post_webhook",side_effect=send):
            transport.post_event(event);transport.post_event(event)
        self.assertEqual(captured[0][1],captured[1][1])
        for url,body,headers,options in captured:
            auth=json.loads(base64.b64decode(headers["Authorization"].split(" ",1)[1]))
            self.assertEqual(url,"https://chat.mlai.au/events")
            self.assertIn(["payload",hashlib.sha256(body).hexdigest()],auth["tags"])
            self.assertIn(["u",url],auth["tags"])
            self.assertEqual(auth["kind"],27235)
            from coincurve import PublicKeyXOnly
            self.assertTrue(PublicKeyXOnly(bytes.fromhex(auth["pubkey"])).verify(bytes.fromhex(auth["sig"]),bytes.fromhex(auth["id"])))
            self.assertTrue(options["max_response_bytes"]<=16384)
        self.assertNotEqual(captured[0][2]["Authorization"],captured[1][2]["Authorization"])
        self.assertEqual(event["kind"],9);self.assertEqual(event["tags"][0][0],"h")

    def test_dm_open_reissues_command_after_lost_response_and_reuses_returned_channel(self):
        channel=str(uuid4());events=[]
        def send(url,body,headers,**kwargs):
            event=json.loads(body);events.append(event)
            if len(events)==1:raise CallbackEndpointError("timeout")
            return WebhookResponse(200,json.dumps({"event_id":event["id"],"accepted":True,"message":"response:"+json.dumps({"channel_id":channel,"created":False})}).encode())
        with patch.object(transport,"post_webhook",side_effect=send):
            with self.assertRaises(transport.NotificationTransportError):transport.open_dm([RECIPIENT],"notice")
            self.assertEqual(transport.open_dm([RECIPIENT],"notice"),channel)
        self.assertNotEqual(events[0]["id"],events[1]["id"])
        self.assertTrue(all(["p",RECIPIENT] in event["tags"] for event in events))

    def test_wrong_event_signer_or_provider_acceptance_is_never_success(self):
        event=native_crypto.sign_event(native_crypto.signing_key("02"*32),kind=9,created_at=1,tags=[],content="")
        with patch.object(transport,"post_webhook") as send,self.assertRaises(transport.NotificationTransportError):transport.post_event(event)
        send.assert_not_called()
        event=native_crypto.sign_event(native_crypto.signing_key(KEY),kind=9,created_at=1,tags=[],content="")
        for status,value in ((200,{"accepted":True,"event_id":"wrong"}),(403,{"message":"token=credential-canary"}),(503,{})):
            with patch.object(transport,"post_webhook",return_value=WebhookResponse(status,json.dumps(value).encode())):
                with self.assertRaises(transport.NotificationTransportError) as error:transport.post_event(event)
                self.assertNotIn("canary",str(error.exception))

    def test_frozen_resend_request_uses_same_idempotency_key_and_only_safe_ack(self):
        notice={"id":"a"*64,"subject":"Review setup","text":"Review setup","link":"https://chat.mlai.au/my-startup/create?company_id=9"}
        payload=transport.email_payload(SimpleNamespace(route_id="owner@example.test"),notice)
        identifier=str(uuid4())
        with patch.object(transport,"post_webhook",return_value=WebhookResponse(200,json.dumps({"id":identifier}).encode())) as send:
            self.assertEqual(transport.post_email(payload,notice["id"]),identifier)
            with override_settings(RESEND_FROM_EMAIL="Changed <changed@example.test>"):transport.post_email(payload,notice["id"])
        self.assertEqual(send.call_args_list[0].args[1],send.call_args_list[1].args[1])
        self.assertEqual(send.call_args.kwargs["timeout"],10)
        self.assertEqual(send.call_args.args[2]["Idempotency-Key"],"articles-goal/"+"a"*64)
        for value in ({"id":"token=credential-canary"},{"name":"concurrent_idempotent_requests"}):
            with patch.object(transport,"post_webhook",return_value=WebhookResponse(200 if "id" in value else 409,json.dumps(value).encode())):
                with self.assertRaises(transport.NotificationTransportError):transport.post_email(payload,notice["id"])


@override_settings(ARTICLES_GOAL_NOTIFICATIONS_ENABLED=True,ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
class OutboxTests(SimpleTestCase):
    def setUp(self):
        self.now=timezone.now();self.goal=fixtures.GoalRunnerRegressions().operation()
        self.goal.payload.update(company_id="9",requested_by_user_id="4")
        self.goal.state="needs_user";self.goal.receipt["user_action"]={"id":"review_setup","run_id":"setup"}
        self.goal.receipt["goal"]["current_source_sha"]="a"*40
        self.goal.connection.refresh_from_db=Mock()
        self.user=SimpleNamespace(pk=4,email="owner@example.test")

    def notice(self):return outbox.track_transition(self.goal,now=self.now)

    def job(self):
        notice=self.notice()
        return SimpleNamespace(pk=uuid4(),connection=self.goal.connection,generation=self.goal.generation,state="running",attempts=1,created_at=self.now,
            payload={"version":1,"goal_operation_id":str(self.goal.pk),"goal_id":notice["goal_id"],"owner_id":"4","scope":deepcopy(self.goal.receipt["goal"]["scope"]),"notice":notice},receipt={"channels":{}})

    def test_state_entries_deduplicate_polls_but_notify_again_after_real_work(self):
        first=self.notice();self.assertIsNone(self.notice())
        self.goal.state="pending";self.assertIsNone(self.notice());self.assertIsNone(self.notice())
        self.goal.state="needs_user";again=self.notice()
        self.assertNotEqual(first["id"],again["id"]);self.assertEqual((first["entry"],again["entry"]),(1,2))
        self.assertIsNone(self.notice())

    def test_atomic_goal_persistence_reserves_once_and_lost_lease_does_not_enqueue(self):
        self.goal._claimed_at=self.goal.updated_at;self.goal._claimed_goal_revision=self.goal.receipt["goal"]["revision"]
        query=rows()
        with patch.object(prepare.transaction,"atomic",return_value=nullcontext()),patch.object(WebsiteConnectionOperation.objects,"filter",return_value=query),patch.object(WebsiteConnectionOperation.objects,"get_or_create",return_value=(SimpleNamespace(),True)) as reserve:
            self.assertIs(prepare._persist_prepare(self.goal,now=self.now),self.goal)
        self.assertEqual(reserve.call_count,1)
        self.assertEqual(reserve.call_args.kwargs["defaults"]["action"],outbox.ACTION)
        self.goal.state="pending";self.notice();self.goal.state="needs_user";query.update.return_value=0
        with patch.object(prepare.transaction,"atomic",return_value=nullcontext()),patch.object(WebsiteConnectionOperation.objects,"filter",return_value=query),patch.object(WebsiteConnectionOperation.objects,"get_or_create") as reserve:
            self.assertIsNone(prepare._persist_prepare(self.goal,now=self.now))
        reserve.assert_not_called()

    def boundaries(self,job,*,devices=(RECIPIENT,)):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany
        from community_chat.models import CommunityChatDevice
        from .models import NotificationChannel
        stack=ExitStack()
        def operation(**kwargs):return rows(self.goal if kwargs.get("action")=="prepare" else job)
        self.operations=stack.enter_context(patch.object(WebsiteConnectionOperation.objects,"filter",side_effect=operation))
        stack.enter_context(patch.object(get_user_model().objects,"filter",return_value=rows(self.user)))
        self.ownership=stack.enter_context(patch.object(VibeRaisingCompany.objects,"filter",return_value=rows()))
        self.devices=stack.enter_context(patch.object(CommunityChatDevice.objects,"filter",return_value=rows(values=devices)))
        self.email=SimpleNamespace(pk=uuid4(),route_id=self.user.email)
        stack.enter_context(patch.object(NotificationChannel.objects,"filter",return_value=rows(self.email)))
        return stack

    def test_actual_channel_chain_keeps_signed_message_across_lost_ack_and_email_is_independent(self):
        job=self.job();event={"id":"c"*64,"pubkey":"e"*64};seen=[]
        def publish(value):
            seen.append(deepcopy(value))
            if len(seen)==1:raise transport.NotificationTransportError("chat_response_unavailable")
        with self.boundaries(job),patch.object(transport,"open_dm",return_value=str(uuid4())) as open_dm,patch.object(transport,"message_event",return_value=event) as create,patch.object(transport,"post_event",side_effect=publish),patch.object(transport,"post_email",return_value=str(uuid4())) as email:
            first=outbox.deliver_channels(job,now=self.now)
            self.assertEqual(first["channels"]["chat"]["status"],"pending");self.assertEqual(first["channels"]["email"]["status"],"sent")
            outbox.deliver_channels(job,now=self.now+timedelta(minutes=2))
            outbox.deliver_channels(job,now=self.now+timedelta(minutes=4))
        self.assertEqual(seen,[event,event]);create.assert_called_once();open_dm.assert_called_once();email.assert_called_once()

    def test_original_owner_scope_and_goal_changes_fence_every_send(self):
        for mutation in ("generation","deletion","owner","goal","membership"):
            self.setUp();job=self.job()
            with self.boundaries(job),patch.object(transport,"open_dm") as send:
                if mutation=="generation":job.connection.generation+=1
                elif mutation=="deletion":job.connection.blockers=[{"deletion_epoch":1}]
                elif mutation=="owner":job.payload["owner_id"]="5"
                elif mutation=="goal":self.goal.state="cancelled"
                else:self.ownership.return_value.exists.return_value=False
                with self.subTest(mutation=mutation),self.assertRaises(outbox.NotificationFenced):outbox.deliver_channels(job,now=self.now)
                send.assert_not_called()

    def test_email_unknown_ack_stops_before_provider_dedup_window_expires(self):
        job=self.job()
        with self.boundaries(job,devices=()),patch.object(transport,"post_email",side_effect=transport.NotificationTransportError("email_response_unavailable")) as send:
            outbox.deliver_channels(job,now=self.now)
            outbox.deliver_channels(job,now=self.now+timedelta(hours=22))
            final=outbox.deliver_channels(job,now=self.now+timedelta(hours=23))
        self.assertEqual(send.call_count,2);self.assertEqual(final["channels"]["email"]["status"],"uncertain")

    def test_prepare_claim_in_flight_waits_instead_of_sending_or_cancelling(self):
        job=self.job();self.goal.state="running"
        with self.boundaries(job),patch.object(transport,"open_dm") as send:
            with self.assertRaises(transport.NotificationTransportError) as error:outbox.deliver_channels(job,now=self.now)
        self.assertEqual(error.exception.outcome,"goal_observation_pending");send.assert_not_called()

    @override_settings(ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY=KEY,COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",RESEND_API_KEY="synthetic")
    def test_crash_after_native_acceptance_restarts_with_original_signed_event(self):
        job=self.job();accepted=set();received=[];channel=str(uuid4());crashed=False
        def send(url,body,headers,**kwargs):
            event=json.loads(body)
            if event["kind"]==9:accepted.add(event["id"]);received.append(event)
            return WebhookResponse(200,json.dumps({"accepted":True,"event_id":event["id"],"message":"response:"+json.dumps({"channel_id":channel})}).encode())
        def operation(**kwargs):
            query=rows(self.goal if kwargs.get("action")=="prepare" else job)
            def update(**fields):
                nonlocal crashed
                if fields.get("receipt",{}).get("channels",{}).get("chat",{}).get("status")=="sent" and not crashed:
                    crashed=True;raise SystemExit("synthetic worker restart after accepted HTTP")
                return 1
            query.update.side_effect=update
            return query
        with self.boundaries(job),patch.object(WebsiteConnectionOperation.objects,"filter",side_effect=operation),patch.object(transport,"post_webhook",side_effect=send),patch.object(transport,"post_email",return_value=str(uuid4())):
            with self.assertRaises(SystemExit):outbox.deliver_channels(job,now=self.now)
            self.assertEqual(job.receipt["channels"]["chat"]["status"],"pending")
            outbox.deliver_channels(job,now=self.now+timedelta(minutes=2))
        self.assertEqual(len(received),2);self.assertEqual(len(accepted),1);self.assertEqual(received[0],received[1])

    @override_settings(ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY=KEY)
    def test_revoked_recipient_between_open_and_publish_does_not_block_email(self):
        job=self.job()
        def open_dm(*args):
            self.devices.return_value.__getitem__.return_value=[]
            return str(uuid4())
        with self.boundaries(job),patch.object(transport,"open_dm",side_effect=open_dm),patch.object(transport,"post_event") as post,patch.object(transport,"post_email",return_value=str(uuid4())) as email:
            result=outbox.deliver_channels(job,now=self.now)
        post.assert_not_called();email.assert_called_once()
        self.assertEqual(result["channels"]["chat"]["outcome"],"chat_recipient_revoked")
        self.assertEqual(result["channels"]["email"]["status"],"sent")

    def test_claimed_notice_completion_and_disabled_rollout_never_calls_provider(self):
        job=self.job();job.state="pending";job.next_attempt_at=self.now;job.save=Mock()
        with patch.object(outbox.transaction,"atomic",return_value=nullcontext()),patch.object(WebsiteConnectionOperation.objects,"select_for_update",return_value=rows(job)),patch.object(WebsiteConnectionOperation.objects,"filter",return_value=rows()) as saved,patch.object(outbox,"deliver_channels",return_value={"channels":{"chat":{"status":"sent"},"email":{"status":"skipped"}}}) as deliver:
            self.assertEqual(outbox.advance_notice(job.pk,now=self.now),"completed")
            self.assertIsNone(saved.return_value.update.call_args.kwargs["next_attempt_at"])
            job.state="pending";job.next_attempt_at=self.now
            with override_settings(ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False):self.assertIsNone(outbox.advance_notice(job.pk,now=self.now))
        deliver.assert_called_once();self.assertEqual(job.attempts,2)

    @override_settings(RESEND_API_KEY="synthetic")
    def test_actual_email_transport_replay_after_accepted_timeout_uses_one_provider_key(self):
        job=self.job();keys=[];bodies=[];provider_id=str(uuid4())
        def send(url,body,headers,**kwargs):
            self.assertEqual(url,"https://api.resend.com/emails")
            keys.append(headers["Idempotency-Key"]);bodies.append(body)
            if len(keys)==1:raise CallbackEndpointError("timeout")
            return WebhookResponse(200,json.dumps({"id":provider_id}).encode())
        with self.boundaries(job,devices=()),patch.object(transport,"post_webhook",side_effect=send):
            outbox.deliver_channels(job,now=self.now)
            final=outbox.deliver_channels(job,now=self.now+timedelta(minutes=2))
        self.assertEqual(len(keys),2);self.assertEqual(len(set(keys)),1);self.assertEqual(bodies[0],bodies[1])
        self.assertEqual(final["channels"]["email"]["message_id"],provider_id)

    def test_existing_reconciler_routes_notification_rows_to_independent_delivery(self):
        from . import website_reconciliation as reconciliation
        job=self.job();query=rows(values=[job.pk]);query.exclude.return_value=query
        def operations(*args,**kwargs):
            if kwargs.get("action")==outbox.ACTION:return rows(job)
            if kwargs.get("action"):
                absent=rows();absent.exists.return_value=False;return absent
            return query
        with patch.object(WebsiteConnectionOperation.objects,"filter",side_effect=operations) as select,patch("content_factory.website_support.reconcile_support_verifications"),patch.object(reconciliation,"monitor_cleanup_pull_requests"),patch.object(outbox,"advance_notice",return_value="completed") as advance,patch("content_factory.website_prepare.advance_prepare") as site:
            result=reconciliation.process_website_connection_operations(now=self.now)
        self.assertEqual(result["processed"],1);self.assertEqual(result["completed"],1)
        advance.assert_called_once_with(job.pk,now=self.now);site.assert_not_called()
        # Evaluate the real scheduler predicate: a crashed running notice must
        # re-enter after its lease, while a live lease and unrelated task do not.
        from django.db.models import Q
        predicates=next(call.args for call in select.call_args_list if call.args and not call.kwargs.get("action"))
        def matches(node,row):
            def leaf(value):
                key,expected=value;parts=key.split("__");actual=row.get(parts[0])
                return actual is None if parts[-1]=="isnull" and expected else actual is not None if parts[-1]=="isnull" else actual is not None and actual<=expected if parts[-1]=="lte" else actual==expected
            values=[matches(child,row) if isinstance(child,Q) else leaf(child) for child in node.children]
            value=all(values) if node.connector=="AND" else any(values)
            return not value if node.negated else value
        due={"action":outbox.ACTION,"state":"running","next_attempt_at":self.now-timedelta(seconds=1)}
        self.assertTrue(all(matches(item,due) for item in predicates))
        self.assertFalse(all(matches(item,{**due,"next_attempt_at":self.now+timedelta(minutes=1)}) for item in predicates))
        self.assertFalse(all(matches(item,{**due,"action":"unrelated"}) for item in predicates))
        self.assertTrue(all(matches(item,{**due,"action":"goal-refund"}) for item in predicates))
