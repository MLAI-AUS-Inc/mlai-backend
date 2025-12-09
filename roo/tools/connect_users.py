"""
Connect Users Tool for Roo Agent

Searches the Neo4j knowledge graph to find community members
with relevant expertise and suggests them to the requester.
"""
from typing import List, Dict, Any, Optional

from .base import BaseTool, ToolResult
from ..graph import get_relevant_users_for_topics, search_users_by_expertise
from ..llm import get_llm_client, chat
from ..prompts import (
    get_topic_extraction_prompt,
    get_warm_personality_prompt,
    get_no_users_fallback_prompt
)


class ConnectUsersTool(BaseTool):
    """
    Tool for finding and connecting community members based on expertise.
    
    Flow:
    1. Extract topics from user's query using LLM
    2. Query Neo4j for users with relevant expertise
    3. Format warm, personalized response
    """
    
    name = "connect_users"
    description = "Find and suggest community members with relevant expertise for a given topic or question"
    
    def execute(self, query: str, user_id: str, **kwargs) -> ToolResult:
        """
        Execute the connect users flow.
        
        Args:
            query: User's question/request
            user_id: Slack ID of the requesting user (to exclude from results)
            **kwargs: Additional context
        
        Returns:
            ToolResult with user suggestions or fallback message
        """
        print(f"🔗 CONNECT USERS TOOL: Executing for query: '{query[:80]}...'")
        
        try:
            # Step 1: Extract topics from the query
            topics = self._extract_topics(query)
            
            if not topics:
                print("   ⚠️ No topics extracted, using fallback")
                return self._fallback_response(query)
            
            print(f"   📌 Extracted topics: {topics}")
            
            # Step 2: Query Neo4j for relevant users
            users_by_topic = get_relevant_users_for_topics(
                topics=topics,
                exclude_user_id=user_id,
                limit=5
            )
            
            if not users_by_topic:
                print("   ⚠️ No users found, using fallback")
                return self._fallback_response(query)
            
            # Step 3: Deduplicate and rank users
            ranked_users = self._rank_users(users_by_topic)
            
            if not ranked_users:
                return self._fallback_response(query)
            
            print(f"   👥 Found {len(ranked_users)} relevant users")
            
            # Step 4: Generate warm response
            response = self._format_response(query, ranked_users)
            
            return ToolResult(
                success=True,
                data={
                    "topics": topics,
                    "users": ranked_users,
                    "user_count": len(ranked_users)
                },
                message=response
            )
            
        except Exception as e:
            print(f"   ❌ CONNECT USERS TOOL FAILED: {e}")
            import traceback
            traceback.print_exc()
            
            return ToolResult(
                success=False,
                data=None,
                message="Sorry, I had trouble searching for people. Mind trying again?",
                error=str(e)
            )
    
    def _extract_topics(self, query: str) -> List[str]:
        """Extract topics from the user's query using LLM."""
        try:
            response = chat([
                {"role": "system", "content": get_topic_extraction_prompt()},
                {"role": "user", "content": query}
            ], temperature=0.3, max_tokens=200)
            
            # Parse comma-separated topics
            topics = [t.strip() for t in response.split(",") if t.strip()]
            
            # Filter out generic words
            banned = {"help", "please", "anyone", "someone", "thanks", "recommend"}
            topics = [t for t in topics if t.lower() not in banned]
            
            return topics[:5]  # Max 5 topics
            
        except Exception as e:
            print(f"   ❌ Topic extraction failed: {e}")
            return []
    
    def _rank_users(self, users_by_topic: Dict[str, List[Dict]]) -> List[Dict]:
        """
        Deduplicate and rank users across topics.
        
        Prioritizes:
        1. Users who appear in multiple topics
        2. IS_EXPERT_IN > WORKING_ON > INTERESTED_IN
        3. Higher activity levels
        """
        user_scores = {}
        user_data = {}
        
        relationship_scores = {
            "IS_EXPERT_IN": 10,
            "WORKING_ON": 6,
            "INTERESTED_IN": 3,
        }
        
        for topic, users in users_by_topic.items():
            for user in users:
                uid = user.get("user_id")
                if not uid:
                    continue
                
                # Calculate score
                rel_score = relationship_scores.get(user.get("relationship"), 1)
                activity = user.get("activity_level") or 1
                score = rel_score * (1 + 0.1 * min(activity, 10))  # Cap activity bonus
                
                if uid in user_scores:
                    user_scores[uid] += score  # Bonus for multiple topics
                    user_data[uid]["topics"].append(topic)
                    # Keep best relationship
                    if rel_score > relationship_scores.get(user_data[uid]["best_relationship"], 0):
                        user_data[uid]["best_relationship"] = user.get("relationship")
                else:
                    user_scores[uid] = score
                    user_data[uid] = {
                        "user_id": uid,
                        "name": user.get("name", "Unknown"),
                        "best_relationship": user.get("relationship"),
                        "topics": [topic],
                        "matched_topic": user.get("matched_topic", topic)
                    }
        
        # Sort by score and return top users
        sorted_users = sorted(
            user_data.values(),
            key=lambda x: user_scores[x["user_id"]],
            reverse=True
        )
        
        return sorted_users[:3]  # Return top 3
    
    def _format_response(self, query: str, users: List[Dict]) -> str:
        """Generate a warm, personalized response with user suggestions."""
        try:
            # Build user info for the LLM
            user_info = []
            for u in users:
                user_info.append(
                    f"- {u['name']} (ID: {u['user_id']}): {u['best_relationship']} in {', '.join(u['topics'])}"
                )
            
            user_context = "\n".join(user_info)
            
            response = chat([
                {"role": "system", "content": get_warm_personality_prompt()},
                {"role": "user", "content": f"""Original request: "{query}"

Users to suggest:
{user_context}

Generate a warm response tagging these users. Use <@USER_ID> format for tags."""}
            ], temperature=0.7, max_tokens=300)
            
            return response
            
        except Exception as e:
            print(f"   ❌ Response formatting failed: {e}")
            # Fallback to simple format
            mentions = [f"<@{u['user_id']}>" for u in users]
            return f"G'day! You might want to chat with {', '.join(mentions)} about this! 🎯"
    
    def _fallback_response(self, query: str) -> ToolResult:
        """Generate response when no users are found."""
        try:
            response = chat([
                {"role": "system", "content": get_no_users_fallback_prompt()},
                {"role": "user", "content": f"User's request: \"{query}\""}
            ], temperature=0.7, max_tokens=150)
            
            return ToolResult(
                success=True,  # Fallback is still a valid response
                data={"users": [], "topics": []},
                message=response
            )
            
        except Exception:
            return ToolResult(
                success=True,
                data={"users": [], "topics": []},
                message="Hmm, I don't have anyone specific for that one yet 🤔 Anyone in the community keen to help out?"
            )
