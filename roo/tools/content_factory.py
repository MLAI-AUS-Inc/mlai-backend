"""
Content Factory Tool for Roo Agent

Implements a conversational flow:
1. Initial request → Ask clarifying questions
2. User provides context → Acknowledge and start creation
3. Article complete → Format response with preview/PR URLs
"""
import json
import threading
from typing import Dict, Any, Optional

from .base import BaseTool, ToolResult
from ..content_factory_client import ContentFactoryClient
from ..slack_client import post_message, get_thread_messages, get_user_info
from ..llm import chat
from ..prompts import (
    get_content_factory_params_prompt,
    get_content_factory_questions_prompt,
    get_content_factory_context_prompt,
    get_content_factory_success_prompt,
)


class ContentFactoryTool(BaseTool):
    """
    Tool for generating content via the Content Factory pipeline.
    
    Conversational Flow:
    1. Initial Request: Triggers Triage (Direct vs Discovery vs Ambiguous).
    2. Ambiguous: Bot asks "Do you have a topic OR want to find gaps?".
    3. Direct Mode: 
       - If params missing -> Ask clarifying questions.
       - If params ready -> Call /generate.
    4. Discovery Mode:
       - If competitors missing -> Ask.
       - Call /discover -> Show opportunities.
       - User selection -> Call /generate.
    """
    
    name = "content_factory"
    description = "Generate SEO/AEO articles. Supports 'Direct' (topic known) vs 'Discovery' (need ideas) modes."
    
    def execute(self, query: str, user_id: str, channel_id: str, thread_ts: str, **kwargs) -> ToolResult:
        """
        Execute the content factory flow: Triage -> Determine State -> Act.
        """
        print(f"🏭 CONTENT FACTORY TOOL: Executing for query: '{query[:80]}...'")
        
        try:
            # Step 1: Get thread history
            thread_messages = get_thread_messages(channel_id, thread_ts)
            
            # Step 2: Detect conversation state
            state = self._detect_conversation_state(thread_messages)
            print(f"   📊 Conversation state: {state}")
            
            if state == "initial":
                return self._handle_initial_request(query, user_id)
            elif state == "discovery_selection":
                return self._handle_discovery_selection(thread_messages, user_id, channel_id, thread_ts)
            elif state == "triage_selection":
                return self._handle_triage_selection(query, thread_messages)
            else:
                # Direct follow-up or general context gathering
                return self._handle_direct_followup(
                    query=query,
                    thread_messages=thread_messages,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts
                )
            
        except Exception as e:
            print(f"   ❌ CONTENT FACTORY TOOL FAILED: {e}")
            import traceback
            traceback.print_exc()
            return ToolResult(
                success=False,
                data=None,
                message=f"Sorry mate, something went wrong with the Content Factory: {str(e)}",
                error=str(e)
            )

    def _detect_conversation_state(self, thread_messages: list[dict]) -> str:
        """
        Detects if we are in initial, discovery selection, triage selection, or direct followup state.
        """
        if not thread_messages:
            return "initial"
        
        bot_msgs = [m for m in thread_messages if m.get("is_bot")]
        if not bot_msgs:
            return "initial"
            
        last_bot_msg = bot_msgs[-1]["text"].lower()
        
        # Check if bot presented opportunities to select from
        if "here are some content opportunities" in last_bot_msg or "reply with the number" in last_bot_msg:
            return "discovery_selection"

        # Check if bot asked the Triage Question (Choice)
        # Dynamic message likely contains "topic"/"article" AND "gaps"/"opportunities"
        if ("topic" in last_bot_msg or "article" in last_bot_msg) and ("gap" in last_bot_msg or "opportunit" in last_bot_msg):
             return "triage_selection"
            
        return "followup"

    def _handle_initial_request(self, query: str, user_id: str) -> ToolResult:
        """
        Triages the request into Direct, Discovery, or Ambiguous.
        """
        print("   📝 Initial request - triaging...")
        
        # Triage using LLM
        triage = self._triage_intent(query)
        intent = triage.get("intent", "direct")
        domain = triage.get("domain", "your domain")
        
        print(f"   🎯 Triage result: {intent} for {domain}")
        
        if intent == "ambiguous":
            # Use LLM to ask this naturally in Roo's voice
            try:
                msg = chat([
                    {"role": "system", "content": (
                        "You are Roo, a helpful Australian AI assistant 🦘. "
                        "The user wants content for their site but hasn't specified if they have a topic request "
                        "or if they want you to find ideas (content gaps).\n"
                        "Ask them nicely if they already have a specific article in mind, OR if they would like you "
                        "to analyze competitors to find content opportunities/gaps.\n"
                        "Keep it short, punchy (2-3 sentences max), and friendly."
                    )},
                    {"role": "user", "content": f"I want content for {domain}."}
                ], temperature=0.7, max_tokens=150)
            except Exception:
                # Fallback if LLM fails
                msg = (
                    f"G'day! 🦘 Happy to help with {domain}. "
                    "Do you have a specific topic in mind, or should I look at competitors to find some content gaps for you?"
                )

            return ToolResult(
                success=True,
                data={"state": "asking_intent"},
                message=msg
            )
        elif intent == "discovery":
            return self._start_discovery_flow(query, domain)
        else:
            # Direct flow: check if we have enough info to start immediately
            if triage.get("topic") and triage.get("target_keyword"):
                 return self._start_direct_flow_questions(query, domain)
            else:
                return self._start_direct_flow_questions(query, domain)

    def _triage_intent(self, query: str) -> dict:
        """Determines if user wants ideas (discovery) or has a topic (direct)."""
        prompt = """
        Analyze the user's request.
        Return JSON string:
        {
            "intent": "discovery" | "direct" | "ambiguous",
            "domain": "string (optional)",
            "topic": "string (optional, if user specifies what to write about)",
            "target_keyword": "string (optional)"
        }
        
        Rules:
        - "Write an article" (no topic) -> intent: ambiguous
        - "Content Factory" (no topic) -> intent: ambiguous
        - "Write about AI" -> intent: direct
        - "Find ideas" / "Gaps" -> intent: discovery
        """
        try:
            response = chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": query}
            ], temperature=0.1, max_tokens=150)
            cleaned = response.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
        except:
            return {"intent": "direct"}

    def _handle_triage_selection(self, query: str, thread_messages: list[dict]) -> ToolResult:
        """
        Handle the user's response to the Direct vs Discovery choice.
        """
        # Re-run triage on the USER's response (query) combined with their original intent if needed, 
        # but usually the response "I have an idea" or "Find gaps" is enough.
        
        triage = self._triage_intent(query)
        intent = triage.get("intent", "direct")
        
        # Need domain from previous turn?
        # Try to find domain in thread
        domain = "your site"
        for msg in thread_messages:
            if "**" in msg.get("text", ""):
                try:
                    domain = msg.get("text", "").split("**")[1]
                    break
                except:
                    pass
        
        if intent == "discovery":
             return self._start_discovery_flow(query, domain)
        else:
             # Default to direct
             return self._start_direct_flow_questions(query, domain)

    def _start_discovery_flow(self, query: str, domain: str) -> ToolResult:
        """
        Starts discovery: extract competitors, run discovery, show options.
        """
        # We need competitors. Use extract params to get them if possible.
        params = self._extract_params(query)
        competitors = params.get("competitors", [])
        
        if not competitors:
            return ToolResult(
                success=True,
                data={"state": "asking_competitors"},
                message=f"I can help find content ideas for **{domain}**! 🕵️\n\nWho are your main competitors? (e.g. `competitor1.com, competitor2.com`)"
            )
            
        return self._run_discovery(domain, competitors)

    def _run_discovery(self, domain: str, competitors: list[str]) -> ToolResult:
        """Calls API to discover opportunities and presents them."""
        msg = f"🔍 Analyzing {', '.join(competitors)} to find opportunities for {domain}..."
        
        try:
            client = ContentFactoryClient()
            opportunities = client.discover_opportunities(domain, competitors)
            
            if not opportunities:
                return ToolResult(
                    success=True, 
                    data={"state": "discovery_empty"},
                    message=f"No obvious content gaps found for {domain} against {competitors}. Try different competitors?"
                )
            
            # Format opportunities for display
            display_msg = f"Here are some content opportunities for **{domain}**:\n\n"
            for idx, opp in enumerate(opportunities[:5], 1):
                display_msg += f"*{idx}. {opp['keyword'].title()}*\n"
                display_msg += f"   Volume: {opp.get('volume', 'N/A')} | Diff: {opp.get('difficulty', 'N/A')} | Intent: {opp.get('intent', 'N/A')}\n\n"
            
            display_msg += "Reply with the number (e.g. `1`) to generate that article, or type your own topic!"
            
            # Store opportunities in data? The next turn won't have this data object.
            # We rely on the thread history or we need to re-fetch/cache. 
            # Ideally the bot reads its own message to map number back to keyword.
            return ToolResult(
                success=True,
                data={"state": "discovery_options_presented", "opportunities": opportunities}, # Not persisted state, but informational
                message=display_msg
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                message=f"Failed to run discovery: {str(e)}"
            )

    def _handle_discovery_selection(self, thread_messages: list[dict], user_id: str, channel_id: str, thread_ts: str) -> ToolResult:
        """
        User selected an option from discovery list.
        """
        # Get user's selection (last message)
        last_msg = thread_messages[-1]["text"].strip()
        
        # Get the bot's previous message with the list
        bot_msgs = [m for m in thread_messages if m.get("is_bot")]
        last_bot_msg = bot_msgs[-1]["text"]
        
        # Try to parse number
        try:
            selection_idx = int(last_msg) - 1
            # Extract keyword from bot message
            # Lines look like: "*1. Keyword Title*"
            lines = last_bot_msg.split("\n")
            option_lines = [l for l in lines if l.startswith("*") and "." in l]
            
            if 0 <= selection_idx < len(option_lines):
                target_line = option_lines[selection_idx]
                target_keyword = target_line.split(".", 1)[1].replace("*", "").strip().lower()
                topic = target_keyword.title() # Use keyword as topic
                
                # We need domain. It should be in bot message "for **domain**"
                domain = "your site"
                if "**" in last_bot_msg:
                    domain = last_bot_msg.split("**")[1]

                return self._trigger_generation(
                    domain=domain,
                    topic=topic,
                    target_keyword=target_keyword,
                    context=self._format_thread_for_llm(thread_messages),
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    user_id=user_id
                )
        except ValueError:
            pass
            
        # If not a number, maybe they typed a topic manually? treated as direct
        return self._handle_direct_followup(last_msg, thread_messages, user_id, channel_id, thread_ts)

    def _start_direct_flow_questions(self, query: str, domain: str) -> ToolResult:
        """Ask clarifying questions for direct generation."""
        # Use existing logic but simplifed
        return ToolResult(
            success=True,
            data={"state": "asking_questions", "domain": domain},
            message=(
                f"G'day! 🦘 Happy to write for **{domain}**.\n\n"
                f"What's the specific topic or keyword you'd like to target?\n"
                f"Any key competitors I should look at for context?"
            )
        )

    def _handle_direct_followup(self, query: str, thread_messages: list[dict], user_id: str, channel_id: str, thread_ts: str) -> ToolResult:
        """
        Extracts details and triggers generation.
        """
        conversation = self._format_thread_for_llm(thread_messages)
        
        # We need to distinguish between "providing competitors for discovery" and "providing topic for generation"
        # Check if the OTHER party (bot) just asked for competitors for discovery
        bot_msgs = [m for m in thread_messages if m.get("is_bot")]
        if bot_msgs:
            last_bot_msg = bot_msgs[-1]["text"]
            if "Who are your main competitors" in last_bot_msg and "find content ideas" in last_bot_msg:
                # User just provided competitors for discovery!
                # Extract competitors from User message
                # For simplicity, treat the whole message as competitor list string or use LLM extraction
                params = self._extract_params(query)
                competitors = params.get("competitors", [query])
                
                # Where is the domain? We need to find it from previous context
                domain_matches = [m for m in thread_messages if "domain" in m.get("text", "").lower()]
                # Hacky: Try to extract domain from bot's ask message "find content ideas for **domain**"
                domain = "your site"
                if "**" in last_bot_msg:
                    domain = last_bot_msg.split("**")[1]
                    
                return self._run_discovery(domain, competitors)

        # Normal Direct article generation flow
        context = self._extract_context_from_thread(conversation)
        
        domain = context.get("domain")
        topic = context.get("topic_preference")
        target_keyword = context.get("keywords", [None])[0] if context.get("keywords") else topic
        
        if not domain or not topic:
             return ToolResult(
                success=False, 
                data=None,
                message="I still need a **domain** and a **topic**. Could you clarify?"
            )
            
        return self._trigger_generation(
            domain=domain,
            topic=topic,
            target_keyword=target_keyword,
            context=json.dumps(context), # Pass full context object as string
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id
        )

    def _trigger_generation(self, domain: str, topic: str, target_keyword: str, context: str, channel_id: str, thread_ts: str, user_id: str) -> ToolResult:
        """Starts the async generation process."""
        
        ack_message = (
            f"🚀 **On it!**\n\n"
            f"Generating article for **{domain}**\n"
            f"📝 **Topic:** {topic}\n"
            f"🔑 **Keyword:** {target_keyword}\n\n"
            f"I'll ping you when it's done! 🦘"
        )
        
        # Start Thread
        t = threading.Thread(
            target=self._async_generate_job,
            args=(domain, topic, target_keyword, context, channel_id, thread_ts, user_id),
            daemon=True
        )
        t.start()
        
        return ToolResult(success=True, data={"state": "generating_async"}, message=ack_message)

    def _async_generate_job(self, domain, topic, target_keyword, context, channel_id, thread_ts, user_id):
        try:
            client = ContentFactoryClient()
            job_id = client.generate_article(domain, topic, target_keyword, context)
            
            # Track state to avoid spamming Slack
            last_sent_progress = -1
            
            def progress_callback(status_data):
                nonlocal last_sent_progress
                progress = status_data.get("progress", 0)
                step = status_data.get("current_step", "processing")
                
                # Only send update if progress has changed
                if progress > last_sent_progress:
                    # Map steps to friendly emojis/text
                    step_map = {
                        "research": "Doing deep research... 📚",
                        "writing": "Drafting the article... ✍️",
                        "seo": "Optimizing for SEO... 🔍",
                        "critique": "Reviewing and refining... 🧐",
                        "completed": "Finishing up! ✨"
                    }
                    step_msg = step_map.get(step, f"Status: {step}")
                    
                    msg = f"⏳ **Progress Update:** {progress}% - {step_msg}"
                    post_message(channel_id, msg, thread_ts)
                    last_sent_progress = progress

            # Use poll_and_wait helper with callback
            result = client.poll_and_wait(job_id, on_progress=progress_callback)
            
            # New Step: Publish
            publish_result = {}
            try:
                publish_result = client.publish_article(job_id)
                result["publish"] = publish_result
            except Exception as pub_e:
                print(f"Publish failed: {pub_e}")
                result["publish_error"] = str(pub_e)

            # Save to DB
            try:
                self._save_result_to_db(user_id, result, domain)
            except Exception as db_e:
                print(f"DB Save failed: {db_e}")

            # Notify User
            msg = self._format_success_message(result)
            post_message(channel_id, msg, thread_ts)

        except Exception as e:
            error_msg = f"😅 Failed to generate article: {str(e)}"
            post_message(channel_id, error_msg, thread_ts)

    # ... Helper methods (_format_success_message, _extract_params, etc) need to be preserved or updated ...
    # I replaced the whole class, so I need to include them.

    def _format_success_message(self, result: dict) -> str:
        """Format success message."""
        topic = result.get("topic", "your article")
        publish_data = result.get("publish", {})
        
        preview_url = publish_data.get("preview_url")
        pr_url = publish_data.get("pr_url")
        
        if publish_data.get("success"):
            if preview_url:
                return (
                    f"🚀 **Ripper! Content is live!**\n\n"
                    f"📱 **Live Preview:** {preview_url}\n"
                    f"📝 **Pull Request:** {pr_url}\n\n"
                    f"Topic: {topic}"
                )
            else:
                return (
                    f"🚀 **Content created and PR is up!**\n\n"
                    f"📝 **Pull Request:** {pr_url}\n"
                    f"Topic: {topic}"
                )
        else:
             return f"✅ **Content Generated!**\nTopic: {topic}\n(Publish failed: {result.get('publish_error')})"

    def _extract_params(self, query: str) -> Dict[str, Any]:
        """Extract domain and competitors using LLM."""
        try:
            response = chat([
                {"role": "system", "content": get_content_factory_params_prompt()},
                {"role": "user", "content": query}
            ], temperature=0.1, max_tokens=150)
            cleaned = response.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
        except Exception:
            return {}

    def _extract_context_from_thread(self, conversation: str) -> dict:
        """Extract article requirements from thread conversation using LLM."""
        try:
            response = chat([
                {"role": "system", "content": get_content_factory_context_prompt()},
                {"role": "user", "content": conversation}
            ], temperature=0.1, max_tokens=300)
            cleaned = response.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
        except Exception:
            return {}

    def _format_thread_for_llm(self, thread_messages: list[dict]) -> str:
        lines = []
        for msg in thread_messages:
            speaker = "Bot" if msg.get("is_bot") else "User"
            text = msg.get("text", "").strip()
            if text:
                lines.append(f"{speaker}: {text}")
        return "\n".join(lines)

    def _save_result_to_db(self, slack_user_id: str, result: dict, domain: str):
        """Save the generated article result to the database."""
        from django.contrib.auth import get_user_model
        from roo.models import ArticleGeneration
        
        User = get_user_model()
        user_info = get_user_info(slack_user_id)
        email = user_info.get("email")
        
        if not email:
            print(f"⚠️ Could not identify user {slack_user_id}")
            return

        user, _ = User.objects.get_or_create(
            email=email,
            defaults={
                "slack_id": slack_user_id,
                "first_name": user_info.get("name", "").split()[0]
            }
        )
        
        if not user.slack_id:
            user.slack_id = slack_user_id
            user.save()

        res_data = result.get("result", result)
        
        ArticleGeneration.objects.create(
            user=user,
            job_id=result.get("job_id"),
            domain=domain,
            topic=res_data.get("topic"),
            slug=res_data.get("slug"),
            category=res_data.get("category"),
            title=res_data.get("title"),
            meta_title=res_data.get("meta_title"),
            meta_description=res_data.get("meta_description"),
            keywords=res_data.get("keywords", []),
            status='completed',
        )

