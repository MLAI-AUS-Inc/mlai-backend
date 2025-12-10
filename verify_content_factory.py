
import sys
import unittest
from unittest.mock import MagicMock, patch
import json
import os

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
import django
django.setup()

from roo.tools.content_factory import ContentFactoryTool, ToolResult

class TestContentFactoryTool(unittest.TestCase):
    
    def setUp(self):
        self.tool = ContentFactoryTool()
        self.user_id = "U123"
        self.channel_id = "C123"
        self.thread_ts = "123456.789"

    @patch("roo.tools.content_factory.ContentFactoryClient")
    @patch("roo.tools.content_factory.chat")
    @patch("roo.tools.content_factory.get_thread_messages")
    @patch("roo.tools.content_factory.post_message")
    @patch("roo.tools.content_factory.get_user_info")
    @patch("roo.models.ArticleGeneration.objects.create")
    @patch("django.contrib.auth.get_user_model")
    def test_direct_flow_known_topic(self, mock_user_model, mock_create_article, mock_user_info, mock_post, mock_thread, mock_chat, mock_client_cls):
        """Test user asks for specific topic directly."""
        print("\n--- Testing Direct Flow (Known Topic) ---")
        
        # Mocks
        mock_thread.return_value = [] # Initial state
        
        # LLM Triage says "direct"
        mock_chat.side_effect = [
            '{"intent": "direct", "domain": "mlai.au", "topic": "AI Ethics", "target_keyword": "ai ethics"}', # Triage
            '{"domain": "mlai.au"}' # Extract params (if needed)
        ]

        # Execute
        result = self.tool.execute(
            query="Write an article about AI Ethics for mlai.au",
            user_id=self.user_id,
            channel_id=self.channel_id,
            thread_ts=self.thread_ts
        )

        print(f"Result Message: {result.message}")
        self.assertIn("Happy to write for **mlai.au**", result.message)
        self.assertEqual(result.data["state"], "asking_questions")

    @patch("roo.tools.content_factory.ContentFactoryClient")
    @patch("roo.tools.content_factory.chat")
    @patch("roo.tools.content_factory.get_thread_messages")
    @patch("roo.tools.content_factory.post_message")
    def test_discovery_flow_start(self, mock_post, mock_thread, mock_chat, mock_client_cls):
        """Test user asks for ideas."""
        print("\n--- Testing Discovery Flow (Start) ---")
        
        mock_thread.return_value = []
        
        # Triage says "discovery", then extract params empty
        mock_chat.side_effect = [
             '{"intent": "discovery", "domain": "mlai.au"}',
             '{"competitors": []}'
        ]

        result = self.tool.execute(
            query="I need content ideas for mlai.au",
            user_id=self.user_id,
            channel_id=self.channel_id,
            thread_ts=self.thread_ts
        )
        
        print(f"Result Message: {result.message}")
        self.assertIn("Who are your main competitors", result.message)
        self.assertEqual(result.data["state"], "asking_competitors")

    @patch("roo.tools.content_factory.ContentFactoryClient")
    @patch("roo.tools.content_factory.chat")
    @patch("roo.tools.content_factory.get_thread_messages")
    @patch("roo.tools.content_factory.post_message")
    def test_discovery_flow_run(self, mock_post, mock_thread, mock_chat, mock_client_cls):
        """Test user provided competitors, running discovery."""
        print("\n--- Testing Discovery Flow (Run) ---")
        
        # User replies with competitors. Previous message was bot asking.
        mock_thread.return_value = [
            {"is_bot": True, "text": "Who are your main competitors for **mlai.au** to find content ideas?"},
            {"is_bot": False, "text": "competitor1.com"}
        ]
        
        # Extract params called to get competitors from "competitor1.com"
        mock_chat.side_effect = [
             '{"competitors": ["competitor1.com"]}'
        ]
        
        # Mock Client
        mock_client = mock_client_cls.return_value
        mock_client.discover_opportunities.return_value = [
            {"keyword": "cool ai topic", "volume": 100, "difficulty": 10, "intent": "info"}
        ]

        result = self.tool.execute(
            query="competitor1.com",
            user_id=self.user_id,
            channel_id=self.channel_id,
            thread_ts=self.thread_ts
        )
        
        print(f"Result Message: {result.message}")
        self.assertIn("Here are some content opportunities", result.message)
        self.assertIn("Cool Ai Topic", result.message)
        
    @patch("roo.tools.content_factory.ContentFactoryClient")
    @patch("roo.tools.content_factory.chat")
    @patch("roo.tools.content_factory.get_thread_messages")
    @patch("roo.tools.content_factory.post_message")
    @patch("roo.models.ArticleGeneration.objects.create")
    @patch("roo.tools.content_factory.get_user_info")
    @patch("django.contrib.auth.get_user_model")
    def test_discovery_selection_trigger(self, mock_user_class, mock_user_info, mock_create, mock_post, mock_thread, mock_chat, mock_client_cls):
        """Test user selects an option."""
        print("\n--- Testing Discovery Selection ---")
        
        # User selects "1"
        mock_thread.return_value = [
            {"is_bot": True, "text": "Here are some content opportunities for **mlai.au**:\n\n*1. Cool Ai Topic*\n..."},
            {"is_bot": False, "text": "1"}
        ]
        
        # Mock Client for generation
        mock_client = mock_client_cls.return_value
        mock_client.generate_article.return_value = "job-123"
        mock_client.poll_and_wait.return_value = {
            "result": {"topic": "Cool Ai Topic", "slug": "cool-ai-topic"},
            "publish": {"success": True, "pr_url": "http://pr"}
        }
        
        # Mock User Info
        mock_user_info.return_value = {"email": "test@example.com", "name": "Test User"}
        
        with patch("threading.Thread") as mock_thread_cls:
            result = self.tool.execute(
                query="1",
                user_id=self.user_id,
                channel_id=self.channel_id,
                thread_ts=self.thread_ts
            )
            
            print(f"Result Message: {result.message}")
            self.assertIn("Generating article for **mlai.au**", result.message)
            self.assertIn("Cool Ai Topic", result.message)

    @patch("roo.tools.content_factory.ContentFactoryClient")
    @patch("roo.tools.content_factory.chat")
    @patch("roo.tools.content_factory.get_thread_messages")
    @patch("roo.tools.content_factory.post_message")
    def test_ambiguous_flow(self, mock_post, mock_thread, mock_chat, mock_client_cls):
        """Test ambiguous request (triage)."""
        print("\n--- Testing Ambiguous Flow ---")
        
        # 1. Initial Request "Write article"
        mock_thread.return_value = []
        
        # chat calls:
        # 1. Triage -> "ambiguous"
        # 2. Natural Language Generation -> "Do you have a topic or want gaps?"
        mock_chat.side_effect = [
            '{"intent": "ambiguous", "domain": "mlai.au"}',
            "G'day! Do you have a topic in mind or should I find gaps?"
        ]
        
        result = self.tool.execute(
            query="Write an article",
            user_id=self.user_id,
            channel_id=self.channel_id,
            thread_ts=self.thread_ts
        )
        print(f"Result 1: {result.message}")
        # Assert against the mocked natural language response
        self.assertIn("find gaps", result.message) 
        self.assertEqual(result.data["state"], "asking_intent")
        
        # 2. User replies "Find gaps"
        mock_thread.return_value = [
             {"is_bot": True, "text": "Do you have a topic in mind or should I find gaps?"},
             {"is_bot": False, "text": "Find gaps"}
        ]
        
        # Triage on second turn
        mock_chat.side_effect = [
            '{"intent": "discovery"}', # Triage "Find gaps"
            '{"competitors": []}'      # Extract params
        ]
        
        result = self.tool.execute(
            query="Find gaps",
            user_id=self.user_id,
            channel_id=self.channel_id,
            thread_ts=self.thread_ts
        )
        print(f"Result 2: {result.message}")
        self.assertIn("Who are your main competitors", result.message)
        self.assertEqual(result.data["state"], "asking_competitors")

if __name__ == "__main__":
    unittest.main()
