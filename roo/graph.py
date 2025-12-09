"""
Neo4j Knowledge Graph Utilities for Roo

Handles connections and queries to the MLAI community knowledge graph.
"""
import os
import time
from typing import Optional, List, Dict, Any
from contextlib import contextmanager


# Lazy-loaded driver
_driver = None


def get_driver():
    """Get Neo4j driver with lazy loading."""
    global _driver
    if _driver is None:
        from neo4j import GraphDatabase
        
        uri = os.environ.get("NEO4J_URI")
        user = os.environ.get("NEO4J_USERNAME", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD")
        
        if not uri:
            raise ValueError("NEO4J_URI environment variable is not set")
        if not password:
            raise ValueError("NEO4J_PASSWORD environment variable is not set")
        
        print(f"🔌 Connecting to Neo4j at {uri}")
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    
    return _driver


@contextmanager
def get_session():
    """Context manager for Neo4j sessions."""
    driver = get_driver()
    session = driver.session()
    try:
        yield session
    finally:
        session.close()


def close_driver():
    """Close the Neo4j driver connection."""
    global _driver
    if _driver:
        _driver.close()
        _driver = None


def get_relevant_users_for_topics(
    topics: List[str],
    exclude_user_id: Optional[str] = None,
    limit: int = 5
) -> Dict[str, List[Dict]]:
    """
    Find the most relevant users for a list of topics.
    
    Prioritizes experts, then active workers, then interested learners.
    EXCLUDES users who only MENTION topics - only includes meaningful relationships.
    
    Args:
        topics: List of topic names to find relevant users for
        exclude_user_id: User ID to exclude from results (e.g., message author)
        limit: Maximum number of users to return per topic
    
    Returns:
        Dictionary mapping topics to lists of relevant users
    """
    start_time = time.time()
    
    print(f"📊 GRAPH QUERY: Finding relevant users for {len(topics)} topics")
    print(f"   Topics: {topics}")
    print(f"   Exclude user: {exclude_user_id}")
    
    results = {}
    
    try:
        with get_session() as session:
            for topic in topics:
                # Query for users with meaningful relationships
                # EXCLUDES MENTIONS - only IS_EXPERT_IN, WORKING_ON, INTERESTED_IN
                query = """
                    MATCH (u:User)-[r]->(t:Topic)
                    WHERE toLower(t.name) CONTAINS toLower($topic_name)
                      AND (u.id IS NULL OR u.id <> $exclude_user_id)
                      AND type(r) IN ['IS_EXPERT_IN', 'WORKING_ON', 'INTERESTED_IN']
                    RETURN u.id as user_id, u.name as name, type(r) as relationship,
                           r.count as activity_level, r.lastMentioned as last_activity,
                           t.name as matched_topic
                    ORDER BY 
                        CASE type(r)
                            WHEN 'IS_EXPERT_IN' THEN 1
                            WHEN 'WORKING_ON' THEN 2
                            WHEN 'INTERESTED_IN' THEN 3
                        END,
                        r.count DESC
                    LIMIT $limit
                """
                
                result = session.run(
                    query,
                    topic_name=topic,
                    exclude_user_id=exclude_user_id or "",
                    limit=limit
                )
                topic_users = [dict(record) for record in result]
                
                if topic_users:
                    results[topic] = topic_users
                    print(f"   Found {len(topic_users)} users for '{topic}'")
                    for user in topic_users[:3]:
                        print(f"     - {user['name']} ({user['relationship']})")
        
        total_time = time.time() - start_time
        print(f"📊 GRAPH QUERY: Complete ({total_time:.2f}s)")
        
        return results
        
    except Exception as e:
        print(f"❌ GRAPH QUERY FAILED: {e}")
        import traceback
        traceback.print_exc()
        return {}


def search_users_by_expertise(
    query: str,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """
    Search for users by expertise area using fuzzy matching.
    
    Args:
        query: Search term (e.g., "AI research", "machine learning")
        limit: Maximum results to return
    
    Returns:
        List of users with their expertise areas
    """
    print(f"🔍 EXPERTISE SEARCH: '{query}'")
    
    try:
        with get_session() as session:
            # Search topics that match the query
            search_query = """
                MATCH (u:User)-[r:IS_EXPERT_IN|WORKING_ON]->(t:Topic)
                WHERE toLower(t.name) CONTAINS toLower($search_term)
                WITH u, t, r, type(r) as rel_type
                ORDER BY 
                    CASE rel_type
                        WHEN 'IS_EXPERT_IN' THEN 1
                        WHEN 'WORKING_ON' THEN 2
                    END,
                    r.count DESC
                WITH u, collect({topic: t.name, relationship: rel_type, count: r.count}) as topics
                RETURN u.id as user_id, u.name as name, topics
                LIMIT $limit
            """
            
            result = session.run(search_query, search_term=query, limit=limit)
            users = [dict(record) for record in result]
            
            print(f"   Found {len(users)} users matching '{query}'")
            
            return users
            
    except Exception as e:
        print(f"❌ EXPERTISE SEARCH FAILED: {e}")
        return []


def get_all_topics(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Get all topics in the knowledge graph with user counts.
    
    Args:
        limit: Maximum topics to return
    
    Returns:
        List of topics with relationship counts
    """
    try:
        with get_session() as session:
            query = """
                MATCH (t:Topic)
                OPTIONAL MATCH (u:User)-[r]->(t)
                WITH t, count(r) as relationship_count, collect(DISTINCT type(r)) as rel_types
                RETURN t.name as topic, relationship_count, rel_types
                ORDER BY relationship_count DESC
                LIMIT $limit
            """
            
            result = session.run(query, limit=limit)
            return [dict(record) for record in result]
            
    except Exception as e:
        print(f"❌ GET TOPICS FAILED: {e}")
        return []


def get_user_expertise(user_id: str) -> List[Dict[str, Any]]:
    """
    Get all topics a user has expertise in.
    
    Args:
        user_id: Slack user ID
    
    Returns:
        List of topics with relationship details
    """
    try:
        with get_session() as session:
            query = """
                MATCH (u:User {id: $user_id})-[r]->(t:Topic)
                RETURN t.name as topic, type(r) as relationship, 
                       r.count as count, r.lastMentioned as last_mentioned
                ORDER BY 
                    CASE type(r)
                        WHEN 'IS_EXPERT_IN' THEN 1
                        WHEN 'WORKING_ON' THEN 2
                        WHEN 'INTERESTED_IN' THEN 3
                        ELSE 4
                    END,
                    r.count DESC
            """
            
            result = session.run(query, user_id=user_id)
            return [dict(record) for record in result]
            
    except Exception as e:
        print(f"❌ GET USER EXPERTISE FAILED: {e}")
        return []
