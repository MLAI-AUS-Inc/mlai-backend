
from unittest import TestCase
from unittest.mock import patch, MagicMock
from datetime import date
from points.services import CoworkingService
from points.models import RewardsCatalog

class CoworkingCostTest(TestCase):
    def setUp(self):
        self.user = MagicMock()
        self.user.id = 1
        self.user.email = 'test@example.com'

    @patch('points.models.RewardsCatalog.objects.get')
    # Use string for settings to avoid import issues if needed, but patch object is fine
    @patch('points.services.settings') 
    def test_get_coworking_cost_with_reward(self, mock_settings, mock_get):
        # Setup mock reward
        mock_reward = MagicMock()
        mock_reward.cost_points = 10
        mock_get.return_value = mock_reward
        
        # Test
        cost = CoworkingService.get_coworking_cost()
        
        # Verify
        self.assertEqual(cost, 10)
        mock_get.assert_called_with(code='COWORKING_DAY', is_active=True)

    @patch('points.models.RewardsCatalog.objects.get')
    @patch('points.services.settings') 
    def test_get_coworking_cost_no_reward_fallback(self, mock_settings, mock_get):
        # Setup no reward
        mock_get.side_effect = RewardsCatalog.DoesNotExist
        
        # Set setting to explicit 4 to verify it falls back to settings
        mock_settings.COWORKING_DAY_COST_POINTS = 4
        
        # Test
        cost = CoworkingService.get_coworking_cost()
        
        # Verify
        self.assertEqual(cost, 4) 


