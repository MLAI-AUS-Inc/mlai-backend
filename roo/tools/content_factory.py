"""
Content Factory Tool for Roo Agent

Allows users to generate SEO/AEO articles by specifying a domain and competitors.
"""
import json
from typing import Dict, Any, Optional

from .base import BaseTool, ToolResult
from ..content_factory_client import ContentFactoryClient
from ..llm import chat
from ..prompts import get_content_factory_params_prompt


class ContentFactoryTool(BaseTool):
    """
    Tool for generating content via the Content Factory pipeline.
    
    Flow:
    1. Extract domain/competitors from query
    2. Call Content Factory API
    3. Return result summary
    """
    
    name = "content_factory"
    description = "Generate SEO/AEO articles for a specific domain and competitors"
    
    def execute(self, query: str, user_id: str, **kwargs) -> ToolResult:
        """
        Execute the content factory flow.
        """
        print(f"🏭 CONTENT FACTORY TOOL: Executing for query: '{query[:80]}...'")
        
        try:
            # Step 1: Extract parameters
            params = self._extract_params(query)
            
            if not params.get("domain"):
                return ToolResult(
                    success=False,
                    data=None,
                    message="I couldn't find a domain name in your request. Could you specify one? (e.g., 'Write an article for example.com')"
                )
            
            domain = params["domain"]
            competitors = params.get("competitors", [])
            
            print(f"   🎯 Domain: {domain}")
            print(f"   ⚔️ Competitors: {competitors}")
            
            # Step 2: Call Content Factory (Blocking)
            client = ContentFactoryClient()
            result = client.generate_article(domain, competitors)
            
            # Step 3: Format Response
            topic = result.get("topic", "New Article")
            slug = result.get("slug", "new-article")
            
            message = (
                f"✅ **Content Generated Successfully!**\n\n"
                f"**Topic:** {topic}\n"
                f"**Slug:** `{slug}`\n\n"
                f"The files have been generated and are ready for review. "
                f"You can find them in the Content Factory output directory."
            )
            
            return ToolResult(
                success=True,
                data=result,
                message=message
            )
            
        except Exception as e:
            print(f"   ❌ CONTENT FACTORY TOOL FAILED: {e}")
            return ToolResult(
                success=False,
                data=None,
                message=f"Sorry, something went wrong with the Content Factory: {str(e)}",
                error=str(e)
            )

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
