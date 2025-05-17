import unittest
import sys
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from otc_data import OTCDataHandler
    from config import (
        ALPHA_VANTAGE_API_KEY,
        OPENEXCHANGERATES_API_KEY,
        CURRENCYLAYER_API_KEY
    )
except ImportError as e:
    logger.error(f"Failed to import required modules: {str(e)}")
    sys.exit(1)

import time

class TestOTCDataHandler(unittest.TestCase):
    def setUp(self):
        try:
            self.handler = OTCDataHandler(api_key=ALPHA_VANTAGE_API_KEY)
        except Exception as e:
            logger.error(f"Failed to initialize OTCDataHandler: {str(e)}")
            raise
        
    def test_realtime_price_fetching(self):
        """Test real-time price fetching from all sources"""
        test_pairs = ['EURUSD_OTC', 'GBPUSD_OTC', 'USDJPY_OTC']
        
        for pair in test_pairs:
            logger.info(f"\nTesting {pair}:")
            try:
                price, source = self.handler.get_realtime_price(pair, return_source=True)
                
                self.assertIsNotNone(price, f"Failed to get price for {pair}")
                self.assertIsNotNone(source, f"Failed to get source for {pair}")
                logger.info(f"Price: {price}, Source: {source}")
                
                # Test price validation
                self.assertTrue(price > 0, f"Invalid price for {pair}: {price}")
                
            except Exception as e:
                logger.error(f"Error testing {pair}: {str(e)}")
                raise
                
            # Add small delay to respect rate limits
            time.sleep(1)
            
    def test_historical_data(self):
        """Test historical data fetching"""
        test_pair = 'EURUSD_OTC'
        
        try:
            data = self.handler.get_historical_data(test_pair, interval='1min')
            self.assertIsNotNone(data, "Failed to get historical data")
            self.assertFalse(data.empty, "Historical data is empty")
            
            # Test technical indicators
            indicators = self.handler.calculate_technical_indicators(data)
            self.assertIn('sma_20', indicators, "Missing SMA indicator")
            self.assertIn('rsi', indicators, "Missing RSI indicator")
            self.assertIn('macd', indicators, "Missing MACD indicator")
            
        except Exception as e:
            logger.error(f"Error testing historical data: {str(e)}")
            raise
        
    def test_rate_limiting(self):
        """Test rate limiting functionality"""
        test_pair = 'EURUSD_OTC'
        
        try:
            # Make multiple requests in quick succession
            for i in range(3):
                price, source = self.handler.get_realtime_price(test_pair, return_source=True)
                self.assertIsNotNone(price, f"Failed to get price on attempt {i+1}")
                logger.info(f"Attempt {i+1}: Price: {price}, Source: {source}")
                time.sleep(0.2)  # Small delay between requests
                
        except Exception as e:
            logger.error(f"Error testing rate limiting: {str(e)}")
            raise
            
if __name__ == '__main__':
    unittest.main() 