"""
Roo Agent - Main Orchestration Layer

The agent receives user messages, triages them using an LLM,
selects the appropriate tool, and generates responses.
"""
import time
from typing import Dict, Any, Optional, List, Type

from .tools.base import BaseTool, ToolResult
from .tools.connect_users import ConnectUsersTool
from .llm import chat, get_llm_client
from .prompts import get_triage_prompt, get_general_response_prompt


class RooAgent:
    """
    Agentic Slack bot that triages requests and executes tools.
    
    Usage:
        agent = RooAgent()
        result = agent.handle_mention(
            text="Do you know anyone in AI research?",
            user_id="U12345",
            channel_id="C67890",
            thread_ts="1234567890.123456"
        )
        # result contains the response message to post
    """
    
    def __init__(self, llm_provider: Optional[str] = None, llm_model: Optional[str] = None):
        """
        Initialize the Roo agent.
        
        Args:
            llm_provider: LLM provider ("gemini", "openai", "anthropic")
            llm_model: Specific model name (uses default if not specified)
        """
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        
        # Initialize LLM client (will use environment defaults if not specified)
        if llm_provider or llm_model:
            self._llm_client = get_llm_client(provider=llm_provider, model=llm_model)
        else:
            self._llm_client = None  # Will use default
        
        # Register available tools
        self.tools: Dict[str, BaseTool] = {
            "connect_users": ConnectUsersTool(),
            # Add more tools here as they're implemented:
            # "assign_points": AssignPointsTool(),
            # "welcome_user": WelcomeUserTool(),
        }
        
        print(f"🤖 Roo Agent initialized with {len(self.tools)} tools")
        print(f"   Tools: {list(self.tools.keys())}")
    
    def handle_mention(
        self,
        text: str,
        user_id: str,
        channel_id: str,
        thread_ts: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Handle an @Roo mention from Slack.
        
        Args:
            text: The message text (with @Roo mention removed)
            user_id: Slack user ID of the requester
            channel_id: Channel where the mention occurred
            thread_ts: Thread timestamp for replying
            **kwargs: Additional context
        
        Returns:
            Dict with:
                - success: bool
                - message: Response text to post
                - tool_used: Which tool was invoked
                - data: Any additional data from the tool
        """
        start_time = time.time()
        
        print(f"\n{'='*60}")
        print(f"🦘 ROO AGENT: Processing mention")
        print(f"   User: {user_id}")
        print(f"   Channel: {channel_id}")
        print(f"   Message: {text[:100]}...")
        print(f"{'='*60}")
        
        try:
            # Step 1: Clean the message (remove @Roo mention)
            clean_text = self._clean_mention(text)
            print(f"📝 Clean text: {clean_text[:100]}...")
            
            # Step 2: Triage - decide which tool to use
            tool_name = self._triage(clean_text)
            print(f"🎯 Triage result: {tool_name}")
            
            # Step 3: Execute the appropriate tool
            if tool_name == "general_response":
                result = self._general_response(clean_text)
            elif tool_name in self.tools:
                result = self.tools[tool_name].execute(
                    query=clean_text,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    **kwargs
                )
            else:
                print(f"   ⚠️ Unknown tool '{tool_name}', falling back to general response")
                result = self._general_response(clean_text)
                tool_name = "general_response"
            
            total_time = time.time() - start_time
            
            print(f"\n📋 AGENT RESULT:")
            print(f"   Tool: {tool_name}")
            print(f"   Success: {result.success}")
            print(f"   Time: {total_time:.2f}s")
            print(f"   Message preview: {result.message[:100]}...")
            print(f"{'='*60}\n")
            
            return {
                "success": result.success,
                "message": result.message,
                "tool_used": tool_name,
                "data": result.data,
                "execution_time": round(total_time, 2)
            }
            
        except Exception as e:
            total_time = time.time() - start_time
            print(f"❌ AGENT ERROR: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                "success": False,
                "message": "Sorry mate, I ran into a bit of trouble. Mind trying again? 🤔",
                "tool_used": "error",
                "data": {"error": str(e)},
                "execution_time": round(total_time, 2)
            }
    
    def _clean_mention(self, text: str) -> str:
        """Remove @Roo mention and clean up the message."""
        import re
        
        # Remove various forms of @mention
        # <@U12345> format (Slack's internal format)
        # @Roo or @roo plain text
        clean = re.sub(r'<@[A-Z0-9]+>', '', text)
        clean = re.sub(r'@[Rr]oo\b', '', clean)
        clean = re.sub(r'@mlai_bot\b', '', clean, flags=re.IGNORECASE)
        
        # Clean up extra whitespace
        clean = ' '.join(clean.split())
        
        return clean.strip()
    
    def _triage(self, text: str) -> str:
        """Use LLM to decide which tool to use."""
        try:
            response = chat([
                {"role": "system", "content": get_triage_prompt()},
                {"role": "user", "content": text}
            ], temperature=0.1, max_tokens=50)
            
            # Extract tool name from response
            tool_name = response.strip().lower().replace("tool:", "").strip()
            
            # Validate tool exists
            if tool_name in self.tools or tool_name == "general_response":
                return tool_name
            
            # Check for partial matches
            for registered_tool in self.tools:
                if registered_tool in tool_name:
                    return registered_tool
            
            return "general_response"
            
        except Exception as e:
            print(f"   ⚠️ Triage failed: {e}, defaulting to connect_users")
            # Default to connect_users for user-finding requests
            lower_text = text.lower()
            if any(word in lower_text for word in ["anyone", "someone", "who", "expert", "know"]):
                return "connect_users"
            return "general_response"
    
    def _general_response(self, text: str) -> ToolResult:
        """Generate a general conversational response."""
        try:
            response = chat([
                {"role": "system", "content": get_general_response_prompt()},
                {"role": "user", "content": text}
            ], temperature=0.7, max_tokens=300)
            
            return ToolResult(
                success=True,
                data={"type": "general"},
                message=response
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                message="G'day! How can I help you today? 🦘",
                error=str(e)
            )
    
    def get_available_tools(self) -> List[Dict[str, str]]:
        """Get list of available tools with their descriptions."""
        return [
            {"name": name, "description": tool.description}
            for name, tool in self.tools.items()
        ]
    
    def register_tool(self, tool: BaseTool):
        """Register a new tool with the agent."""
        self.tools[tool.name] = tool
        print(f"🔧 Registered tool: {tool.name}")


# Convenience function for quick usage
_agent: Optional[RooAgent] = None


def get_agent(
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None
) -> RooAgent:
    """Get or create the singleton Roo agent."""
    global _agent
    if _agent is None:
        _agent = RooAgent(llm_provider=llm_provider, llm_model=llm_model)
    return _agent


def handle_mention(text: str, user_id: str, channel_id: str, thread_ts: str, **kwargs) -> Dict[str, Any]:
    """Convenience function to handle a mention."""
    agent = get_agent()
    return agent.handle_mention(text, user_id, channel_id, thread_ts, **kwargs)
