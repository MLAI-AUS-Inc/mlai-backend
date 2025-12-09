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
from ..slack_client import post_message, get_thread_messages
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
    1. User requests article → Bot asks clarifying questions
    2. User provides context → Bot acknowledges and starts async generation
    3. Article complete → Bot posts success message with URLs
    """
    
    name = "content_factory"
    description = "Generate SEO/AEO articles for a specific domain and competitors"
    
    def execute(self, query: str, user_id: str, channel_id: str, thread_ts: str, **kwargs) -> ToolResult:
        """
        Execute the content factory flow with conversational state detection.
        """
        print(f"🏭 CONTENT FACTORY TOOL: Executing for query: '{query[:80]}...'")
        
        try:
            # Step 1: Get thread history to determine state
            thread_messages = get_thread_messages(channel_id, thread_ts)
            
            # Step 2: Detect conversation state
            state = self._detect_conversation_state(thread_messages)
            print(f"   📊 Conversation state: {state}")
            
            if state == "initial":
                # First message - ask clarifying questions
                return self._handle_initial_request(query)
            else:
                # Follow-up - gather context and create article
                return self._handle_followup_request(
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
        Detect the current state of the conversation.
        
        States:
        - initial: First request, no bot replies yet asking questions
        - followup: Bot has asked questions, user has replied with context
        """
        if not thread_messages:
            return "initial"
        
        # Check if bot has already replied in this thread
        bot_replied = False
        for msg in thread_messages:
            if msg.get("is_bot"):
                # Check if this was a question-asking message (contains question marks or key phrases)
                text = msg.get("text", "").lower()
                if any(phrase in text for phrase in ["competitor", "topic", "angle", "audience", "keywords", "?"]):
                    bot_replied = True
                    break
        
        if not bot_replied:
            return "initial"
        
        # Bot has asked questions - check if there are human replies after
        found_bot_question = False
        for msg in thread_messages:
            if msg.get("is_bot"):
                text = msg.get("text", "").lower()
                if any(phrase in text for phrase in ["competitor", "topic", "angle", "?"]):
                    found_bot_question = True
            elif found_bot_question and not msg.get("is_bot"):
                # Human replied after bot's question
                return "followup"
        
        return "followup"  # Default to followup if bot has engaged

    def _handle_initial_request(self, query: str) -> ToolResult:
        """
        Handle the initial request by asking clarifying questions.
        """
        print("   📝 Initial request - asking clarifying questions")
        
        # Extract domain from query first
        params = self._extract_params(query)
        domain = params.get("domain", "your domain")
        
        # Generate clarifying questions using LLM
        try:
            questions = chat([
                {"role": "system", "content": get_content_factory_questions_prompt()},
                {"role": "user", "content": f"User request: {query}\nDomain: {domain}"}
            ], temperature=0.7, max_tokens=200)
            
            return ToolResult(
                success=True,
                data={"state": "asking_questions", "domain": domain},
                message=questions
            )
        except Exception as e:
            # Fallback to a default question message
            print(f"   ⚠️ LLM question generation failed: {e}")
            return ToolResult(
                success=True,
                data={"state": "asking_questions", "domain": domain},
                message=(
                    f"G'day! 🦘 Happy to create some content for {domain}! "
                    f"Quick questions before I get started:\n\n"
                    f"• Who are your main competitors?\n"
                    f"• Any particular topic or angle you'd like the article to focus on?\n\n"
                    f"Just reply here and I'll get cracking!"
                )
            )

    def _handle_followup_request(
        self,
        query: str,
        thread_messages: list[dict],
        user_id: str,
        channel_id: str,
        thread_ts: str
    ) -> ToolResult:
        """
        Handle a follow-up request by extracting context and starting article creation.
        """
        print("   📝 Follow-up request - extracting context and starting creation")
        
        # Step 1: Build conversation context
        conversation = self._format_thread_for_llm(thread_messages)
        
        # Step 2: Extract requirements from conversation
        context = self._extract_context_from_thread(conversation)
        
        domain = context.get("domain")
        if not domain:
            # Try to extract from the original query
            params = self._extract_params(query)
            domain = params.get("domain")
        
        if not domain:
            return ToolResult(
                success=False,
                data=None,
                message="I couldn't find a domain name in our conversation. Could you specify one? (e.g., 'example.com')"
            )
        
        competitors = context.get("competitors", [])
        topic_preference = context.get("topic_preference")
        target_audience = context.get("target_audience")
        keywords = context.get("keywords", [])
        additional_context = context.get("additional_context")
        
        print(f"   🎯 Domain: {domain}")
        print(f"   ⚔️ Competitors: {competitors}")
        print(f"   📝 Topic: {topic_preference}")
        
        # Step 3: Send acknowledgment message
        ack_message = (
            f"🚀 Ripper! I've got everything I need.\n\n"
            f"Creating an article for **{domain}**"
        )
        if competitors:
            ack_message += f" (analyzing {', '.join(competitors[:2])})"
        if topic_preference:
            ack_message += f"\n📝 Focus: {topic_preference}"
        ack_message += f"\n\nThis usually takes a few minutes. I'll ping you here when it's ready! 🦘"
        
        # Step 4: Start async generation
        thread = threading.Thread(
            target=self._async_generate_and_respond,
            args=(
                domain,
                competitors,
                topic_preference,
                target_audience,
                keywords,
                additional_context,
                channel_id,
                thread_ts
            ),
            daemon=True
        )
        thread.start()
        
        return ToolResult(
            success=True,
            data={
                "state": "creating",
                "domain": domain,
                "competitors": competitors,
                "topic_preference": topic_preference
            },
            message=ack_message
        )

    def _async_generate_and_respond(
        self,
        domain: str,
        competitors: list[str],
        topic_preference: Optional[str],
        target_audience: Optional[str],
        keywords: Optional[list[str]],
        additional_context: Optional[str],
        channel_id: str,
        thread_ts: str
    ):
        """
        Async function to generate article and post result to Slack.
        """
        try:
            print(f"🏭 Starting async article generation for {domain}")
            
            client = ContentFactoryClient()
            result = client.generate_article(
                domain=domain,
                competitors=competitors,
                topic_preference=topic_preference,
                target_audience=target_audience,
                keywords=keywords,
                additional_context=additional_context,
                auto_publish=True
            )
            
            # Format success message
            message = self._format_success_message(result)
            
            # Post to thread
            post_message(
                channel=channel_id,
                text=message,
                thread_ts=thread_ts
            )
            
            print(f"✅ Article generation complete for {domain}")
            
        except Exception as e:
            print(f"❌ Async generation failed: {e}")
            import traceback
            traceback.print_exc()
            
            # Post error to thread
            post_message(
                channel=channel_id,
                text=f"😅 Sorry mate, hit a snag creating the article: {str(e)}\n\nGive it another go?",
                thread_ts=thread_ts
            )

    def _format_success_message(self, result: dict) -> str:
        """
        Format the success message with preview and PR URLs.
        """
        topic = result.get("topic", "your article")
        publish_data = result.get("publish", {})
        
        preview_url = publish_data.get("preview_url")
        pr_url = publish_data.get("pr_url")
        
        # If we have publish data, format nicely
        if publish_data.get("success"):
            if preview_url:
                message = (
                    f"🚀 **Ripper! Your content is live!**\n\n"
                    f"📱 **Live Preview:** {preview_url}\n"
                    f"📝 **Pull Request:** {pr_url}\n\n"
                    f"The preview is deployed and ready to view, legend! 🦘"
                )
            else:
                message = (
                    f"🚀 **Beauty! Content created and PR is up!**\n\n"
                    f"📝 **Pull Request:** {pr_url}\n\n"
                    f"The preview is still deploying - check the PR for status updates."
                )
        else:
            # Fallback if no publish data
            slug = result.get("slug", "new-article")
            message = (
                f"✅ **Content Generated!**\n\n"
                f"**Topic:** {topic}\n"
                f"**Slug:** `{slug}`\n\n"
                f"The article has been generated and is ready for review."
            )
            
            if result.get("publish_error"):
                message += f"\n\n⚠️ Note: Auto-publish didn't work: {result.get('publish_error')}"
        
        return message

    def _format_thread_for_llm(self, thread_messages: list[dict]) -> str:
        """Format thread messages for LLM context extraction."""
        lines = []
        for msg in thread_messages:
            speaker = "Bot" if msg.get("is_bot") else "User"
            text = msg.get("text", "").strip()
            if text:
                lines.append(f"{speaker}: {text}")
        return "\n".join(lines)

    def _extract_context_from_thread(self, conversation: str) -> dict:
        """Extract article requirements from thread conversation using LLM."""
        try:
            response = chat([
                {"role": "system", "content": get_content_factory_context_prompt()},
                {"role": "user", "content": f"Thread conversation:\n{conversation}"}
            ], temperature=0.1, max_tokens=300)
            
            # Clean and parse JSON
            cleaned = response.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
            
        except Exception as e:
            print(f"   ⚠️ Context extraction failed: {e}")
            return {}

    def _extract_params(self, query: str) -> Dict[str, Any]:
        """Extract domain and competitors using LLM."""
        try:
            response = chat([
                {"role": "system", "content": get_content_factory_params_prompt()},
                {"role": "user", "content": query}
            ], temperature=0.1, max_tokens=150)
            
            # Clean response (sometimes LLMs add markdown code blocks)
            cleaned = response.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
            
        except Exception as e:
            print(f"   ⚠️ Param extraction failed: {e}")
            return {}
