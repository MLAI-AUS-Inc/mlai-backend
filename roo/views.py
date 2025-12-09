"""
Slack Webhook Views for Roo

Handles incoming Slack events and routes them to the agent.
"""
import os
import json
import threading

from django.http import JsonResponse, HttpResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from .agent import get_agent
from .slack_client import post_message, verify_request_signature


@method_decorator(csrf_exempt, name='dispatch')
class SlackEventsView(View):
    """
    Webhook endpoint for Slack Events API.
    
    Handles:
    - url_verification challenges
    - app_mention events (when @Roo is tagged)
    """
    
    def post(self, request):
        """Handle incoming Slack events."""
        # Get signing secret
        signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
        
        # Verify request signature (if secret is configured)
        if signing_secret:
            timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
            signature = request.headers.get("X-Slack-Signature", "")
            
            if not verify_request_signature(
                signing_secret, timestamp, signature, request.body
            ):
                print("❌ Invalid Slack signature")
                return HttpResponse("Invalid signature", status=403)
        
        # Parse event payload
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse("Invalid JSON", status=400)
        
        # Handle URL verification challenge
        if payload.get("type") == "url_verification":
            print("✅ Slack URL verification challenge")
            return JsonResponse({"challenge": payload.get("challenge")})
        
        # Handle events
        event = payload.get("event", {})
        event_type = event.get("type")
        
        print(f"📨 Received Slack event: {event_type}")
        
        if event_type == "app_mention":
            # Process in background to respond quickly
            thread = threading.Thread(
                target=self._handle_mention,
                args=(event,),
                daemon=True
            )
            thread.start()
            return HttpResponse(status=200)

        # Handle 'message' events (e.g. DMs)
        if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
            # If it's a DM or mentions the bot name (fallback)
            is_dm = event.get("channel_type") == "im"
            
            if is_dm:
                print(f"📨 Received DM from {event.get('user')}")
                thread = threading.Thread(
                    target=self._handle_mention,
                    args=(event,),
                    daemon=True
                )
                thread.start()
                return HttpResponse(status=200)
        
        # Acknowledge other events
        return HttpResponse(status=200)
    
    def _handle_mention(self, event: dict):
        """
        Handle an @Roo mention asynchronously.
        
        Args:
            event: Slack event payload
        """
        try:
            user_id = event.get("user")
            text = event.get("text", "")
            channel_id = event.get("channel")
            thread_ts = event.get("thread_ts") or event.get("ts")
            
            print(f"\n🦘 ROO MENTION: from {user_id} in {channel_id}")
            print(f"   Text: {text[:100]}...")
            
            # Get the agent and process the mention
            agent = get_agent()
            result = agent.handle_mention(
                text=text,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts
            )
            
            # Post the response to Slack
            if result.get("message"):
                post_message(
                    channel=channel_id,
                    text=result["message"],
                    thread_ts=thread_ts
                )
            
            print(f"✅ Mention handled successfully (tool: {result.get('tool_used')})")
            
        except Exception as e:
            print(f"❌ Error handling mention: {e}")
            import traceback
            traceback.print_exc()
            
            # Try to post error message
            try:
                post_message(
                    channel=event.get("channel"),
                    text="Sorry mate, I ran into a bit of trouble. Mind trying again? 🤔",
                    thread_ts=event.get("thread_ts") or event.get("ts")
                )
            except Exception:
                pass


@method_decorator(csrf_exempt, name='dispatch')
class SlackCommandsView(View):
    """
    Webhook endpoint for Slack Slash Commands.
    
    Handles commands like /send_welcome
    """
    
    def post(self, request):
        """Handle incoming slash command."""
        # Parse form data (Slack sends commands as form data)
        command = request.POST.get("command", "")
        text = request.POST.get("text", "")
        user_id = request.POST.get("user_id", "")
        channel_id = request.POST.get("channel_id", "")
        response_url = request.POST.get("response_url", "")
        
        print(f"📨 Slash command: {command} from {user_id}")
        print(f"   Text: {text}")
        
        if command == "/send_welcome":
            return self._handle_send_welcome(user_id, text)
        
        # Unknown command
        return JsonResponse({
            "response_type": "ephemeral",
            "text": f"Unknown command: {command}"
        })
    
    def _handle_send_welcome(self, requester_id: str, text: str):
        """Handle /send_welcome command."""
        # TODO: Add admin check from ADMIN_USER_IDS
        admin_ids = os.environ.get("ADMIN_USER_IDS", "").split(",")
        
        if requester_id not in admin_ids:
            return JsonResponse({
                "response_type": "ephemeral",
                "text": "❌ *Access Denied*\n\nOnly administrators can use this command."
            })
        
        if not text.strip():
            return JsonResponse({
                "response_type": "ephemeral",
                "text": (
                    "📋 *Usage*\n\n"
                    "`/send_welcome <@USER|USER_ID>` — Sends a welcome DM.\n\n"
                    "Example: `/send_welcome <@U123ABC456>`"
                )
            })
        
        # Extract user ID from mention
        target_user = text.strip()
        if target_user.startswith("<@") and ">" in target_user:
            # Format: <@U123ABC456> or <@U123ABC456|name>
            target_user = target_user[2:].split("|")[0].rstrip(">")
        
        # TODO: Implement welcome message sending
        return JsonResponse({
            "response_type": "ephemeral",
            "text": f"✅ Would send welcome to <@{target_user}> (not yet implemented in this version)"
        })


@method_decorator(csrf_exempt, name='dispatch')
class HealthCheckView(View):
    """Simple health check endpoint."""
    
    def get(self, request):
        return JsonResponse({
            "status": "ok",
            "service": "roo",
            "message": "G'day! Roo is awake and ready 🦘"
        })
