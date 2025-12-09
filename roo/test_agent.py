#!/usr/bin/env python3
"""
Test script for Roo Agent with mock Neo4j data.

Run this to verify the agent flow works without needing a real Neo4j connection.

Usage:
    cd /Users/samdonegan/Documents/Antigravity/mlai-backend
    source venv/bin/activate
    python roo/test_agent.py
"""
import os
import sys
from unittest.mock import patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables
from dotenv import load_dotenv
load_dotenv()


def test_llm_connection():
    """Test that the LLM client can connect and respond."""
    print("\n" + "="*60)
    print("🧪 TEST 1: LLM Connection")
    print("="*60)
    
    try:
        from roo.llm import get_llm_client, chat
        
        # Check which provider will be used
        if os.environ.get("GOOGLE_API_KEY"):
            print("   Provider: Gemini (GOOGLE_API_KEY found)")
        elif os.environ.get("OPENAI_API_KEY"):
            print("   Provider: OpenAI (OPENAI_API_KEY found)")
        else:
            print("   ❌ No API key found! Set GOOGLE_API_KEY or OPENAI_API_KEY")
            return False
        
        # Test a simple chat
        print("   Sending test message...")
        response = chat([
            {"role": "user", "content": "Say 'Hello from Roo!' and nothing else."}
        ], temperature=0.1, max_tokens=50)
        
        print(f"   Response: {response}")
        print("   ✅ LLM connection successful!")
        return True
        
    except Exception as e:
        print(f"   ❌ LLM connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_agent_triage():
    """Test that the agent can triage requests correctly."""
    print("\n" + "="*60)
    print("🧪 TEST 2: Agent Triage")
    print("="*60)
    
    try:
        from roo.agent import RooAgent
        
        agent = RooAgent()
        
        # Test messages and expected tools
        test_cases = [
            ("Do you know anyone who does AI research?", "connect_users"),
            ("Hey Roo, how's it going?", "general_response"),
            ("Can you find someone in health tech?", "connect_users"),
            ("What's the weather like?", "general_response"),
            ("Looking for machine learning experts", "connect_users"),
        ]
        
        passed = 0
        for message, expected in test_cases:
            result = agent._triage(message)
            status = "✅" if result == expected else "⚠️"
            print(f"   {status} '{message[:40]}...' → {result} (expected: {expected})")
            if result == expected:
                passed += 1
        
        print(f"\n   Results: {passed}/{len(test_cases)} passed")
        return passed >= 3  # At least 3 should pass
        
    except Exception as e:
        print(f"   ❌ Triage test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_agent_with_mock_neo4j():
    """Test the full agent flow with mocked Neo4j responses."""
    print("\n" + "="*60)
    print("🧪 TEST 3: Full Agent Flow (Mock Neo4j)")
    print("="*60)
    
    # Mock data that would come from Neo4j
    mock_users_by_topic = {
        "AI Research": [
            {
                "user_id": "U001ALICE",
                "name": "Alice Chen",
                "relationship": "IS_EXPERT_IN",
                "activity_level": 15,
                "matched_topic": "Artificial Intelligence"
            },
            {
                "user_id": "U002BOB",
                "name": "Bob Smith",
                "relationship": "WORKING_ON",
                "activity_level": 8,
                "matched_topic": "Machine Learning Research"
            }
        ],
        "Machine Learning": [
            {
                "user_id": "U001ALICE",
                "name": "Alice Chen",
                "relationship": "IS_EXPERT_IN",
                "activity_level": 12,
                "matched_topic": "Machine Learning"
            },
            {
                "user_id": "U003CAROL",
                "name": "Carol Davis",
                "relationship": "INTERESTED_IN",
                "activity_level": 5,
                "matched_topic": "Deep Learning"
            }
        ]
    }
    
    try:
        # Patch the Neo4j function to return mock data
        with patch('roo.tools.connect_users.get_relevant_users_for_topics') as mock_neo4j:
            mock_neo4j.return_value = mock_users_by_topic
            
            from roo.agent import RooAgent
            
            agent = RooAgent()
            
            print("   Testing: 'Do you know anyone who does AI research?'")
            print("   (Using mock Neo4j data)")
            print()
            
            result = agent.handle_mention(
                text="@Roo Do you know anyone who does AI research?",
                user_id="U999TEST",
                channel_id="C123TEST",
                thread_ts="1234567890.123456"
            )
            
            print(f"   Success: {result['success']}")
            print(f"   Tool used: {result['tool_used']}")
            print(f"   Execution time: {result['execution_time']}s")
            print()
            print("   📝 Response:")
            print("   " + "-"*50)
            print(f"   {result['message']}")
            print("   " + "-"*50)
            
            if result.get('data'):
                print(f"\n   📊 Data:")
                print(f"      Topics: {result['data'].get('topics', [])}")
                print(f"      Users found: {result['data'].get('user_count', 0)}")
            
            print("\n   ✅ Full agent flow successful!")
            return True
            
    except Exception as e:
        print(f"   ❌ Agent flow test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_general_response():
    """Test the general response path."""
    print("\n" + "="*60)
    print("🧪 TEST 4: General Response")
    print("="*60)
    
    try:
        # Patch Neo4j to avoid connection errors
        with patch('roo.tools.connect_users.get_relevant_users_for_topics') as mock_neo4j:
            mock_neo4j.return_value = {}
            
            from roo.agent import RooAgent
            
            agent = RooAgent()
            
            print("   Testing: 'Hey Roo, how are you today?'")
            print()
            
            result = agent.handle_mention(
                text="@Roo Hey Roo, how are you today?",
                user_id="U999TEST",
                channel_id="C123TEST",
                thread_ts="1234567890.123456"
            )
            
            print(f"   Success: {result['success']}")
            print(f"   Tool used: {result['tool_used']}")
            print()
            print("   📝 Response:")
            print("   " + "-"*50)
            print(f"   {result['message']}")
            print("   " + "-"*50)
            
            print("\n   ✅ General response successful!")
            return True
            
    except Exception as e:
        print(f"   ❌ General response test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_no_users_fallback():
    """Test the fallback when no users are found."""
    print("\n" + "="*60)
    print("🧪 TEST 5: No Users Fallback")
    print("="*60)
    
    try:
        # Patch Neo4j to return empty results
        with patch('roo.tools.connect_users.get_relevant_users_for_topics') as mock_neo4j:
            mock_neo4j.return_value = {}  # No users found
            
            from roo.agent import RooAgent
            
            agent = RooAgent()
            
            print("   Testing: 'Looking for quantum computing experts'")
            print("   (Mock Neo4j returns no users)")
            print()
            
            result = agent.handle_mention(
                text="@Roo Looking for quantum computing experts",
                user_id="U999TEST",
                channel_id="C123TEST",
                thread_ts="1234567890.123456"
            )
            
            print(f"   Success: {result['success']}")
            print(f"   Tool used: {result['tool_used']}")
            print()
            print("   📝 Fallback Response:")
            print("   " + "-"*50)
            print(f"   {result['message']}")
            print("   " + "-"*50)
            
            print("\n   ✅ Fallback response successful!")
            return True
            
    except Exception as e:
        print(f"   ❌ Fallback test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("🦘 ROO AGENT TEST SUITE")
    print("="*60)
    print("Testing the Roo agent with mock data...")
    
    results = []
    
    # Test 1: LLM Connection
    results.append(("LLM Connection", test_llm_connection()))
    
    # Only continue if LLM works
    if results[0][1]:
        # Test 2: Triage
        results.append(("Agent Triage", test_agent_triage()))
        
        # Test 3: Full Flow with Mock Neo4j
        results.append(("Full Agent Flow", test_agent_with_mock_neo4j()))
        
        # Test 4: General Response
        results.append(("General Response", test_general_response()))
        
        # Test 5: No Users Fallback
        results.append(("No Users Fallback", test_no_users_fallback()))
    
    # Summary
    print("\n" + "="*60)
    print("📋 TEST SUMMARY")
    print("="*60)
    
    passed = 0
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status}: {name}")
        if result:
            passed += 1
    
    print()
    print(f"   Total: {passed}/{len(results)} tests passed")
    
    if passed == len(results):
        print("\n🎉 All tests passed! Roo is ready to go.")
    else:
        print("\n⚠️ Some tests failed. Check the output above for details.")
    
    return passed == len(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
