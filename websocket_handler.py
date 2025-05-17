from flask_socketio import SocketIO, emit
from flask import session, request
from datetime import datetime, timedelta
import json
import logging
from typing import Dict, Optional
import yfinance as yf
import pandas as pd
import numpy as np
import time
import random
import asyncio

logger = logging.getLogger(__name__)

REAL_FOREX_SYMBOLS = {'AUDUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'USDCAD', 'USDCHF', 'NZDUSD', 'EURGBP', 'EURJPY', 'GBPJPY'}
REAL_INDIAN_SYMBOLS = {'NIFTY50', 'BANKNIFTY', 'NSEBANK', 'NSEIT', 'NSEINFRA', 'NSEPHARMA', 'NSEFMCG', 'NSEMETAL', 'NSEENERGY', 'NSEAUTO', 'NIFTYMIDCAP', 'NIFTYSMALLCAP', 'NIFTYNEXT50', 'NIFTY100', 'NIFTY500', 'NIFTYREALTY', 'NIFTYPVTBANK', 'NIFTYPSUBANK', 'NIFTYFIN', 'NIFTYMEDIA', 'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'HINDUNILVR', 'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'BAJFINANCE'}

class WebSocketHandler:
    def __init__(self, socketio):
        self.socketio = socketio
        self.connected_users = {}
        self.subscribed_symbols = {}
        self.price_cache = {}
        self.last_update = {}
        self.last_request_time = {}
        self.min_request_interval = 2  # Reduced to 2 seconds for better responsiveness
        self.rate_limit_backoff = {}
        self.max_backoff = 30
        self.forex_symbols = {
            'AUDUSD': 'AUDUSD=X',
            'EURUSD': 'EURUSD=X',
            'GBPUSD': 'GBPUSD=X',
            'USDJPY': 'USDJPY=X',
            'USDCAD': 'USDCAD=X',
            'USDCHF': 'USDCHF=X',
            'NZDUSD': 'NZDUSD=X',
            'EURGBP': 'EURGBP=X',
            'EURJPY': 'EURJPY=X',
            'GBPJPY': 'GBPJPY=X'
        }
        # Initialize OTC data handler
        from otc_data import OTCDataHandler
        from config import ALPHA_VANTAGE_API_KEY
        self.otc_handler = OTCDataHandler(api_key=ALPHA_VANTAGE_API_KEY)
        self.setup_handlers()
        
    def setup_handlers(self):
        """Setup WebSocket event handlers"""
        @self.socketio.on('connect')
        def handle_connect():
            if 'user_id' in session:
                self.handle_connect(session['user_id'])
            
        @self.socketio.on('disconnect')
        def handle_disconnect():
            if 'user_id' in session:
                self.handle_disconnect(session['user_id'])
            
        @self.socketio.on('subscribe')
        def handle_subscribe(data):
            try:
                symbol = data.get('symbol')
                if symbol and 'user_id' in session:
                    self.subscribe_symbol(session['user_id'], symbol)
            except Exception as e:
                logger.error(f'Error handling subscription: {str(e)}')
                
        @self.socketio.on('unsubscribe')
        def handle_unsubscribe(data):
            try:
                symbol = data.get('symbol')
                if symbol and 'user_id' in session:
                    self.unsubscribe_symbol(session['user_id'], symbol)
            except Exception as e:
                logger.error(f'Error handling unsubscription: {str(e)}')
                
    def handle_connect(self, user_id):
        """Handle new WebSocket connection with improved error handling."""
        try:
            self.connected_users[user_id] = {
                'sid': request.sid,
                'subscriptions': set(),
                'last_update': {},
                'error_count': 0
            }
            logger.info(f"User {user_id} connected via WebSocket")
            return True
        except Exception as e:
            logger.error(f"Error handling WebSocket connection: {str(e)}")
            return False

    def handle_disconnect(self, user_id):
        """Handle WebSocket disconnection with cleanup."""
        try:
            if user_id in self.connected_users:
                # Clean up subscriptions
                for symbol in self.connected_users[user_id]['subscriptions']:
                    self.unsubscribe_symbol(user_id, symbol)
                del self.connected_users[user_id]
                logger.info(f"User {user_id} disconnected from WebSocket")
        except Exception as e:
            logger.error(f"Error handling WebSocket disconnection: {str(e)}")

    async def subscribe_symbol(self, user_id, symbol):
        """Subscribe user to symbol updates"""
        try:
            if user_id not in self.connected_users:
                logger.warning(f"User {user_id} not connected")
                return False

            # Extract symbol string if it's a SQLite Row object
            if hasattr(symbol, 'symbol'):
                symbol = symbol.symbol
            elif isinstance(symbol, dict) and 'symbol' in symbol:
                symbol = symbol['symbol']

            # Add to user's subscriptions
            self.connected_users[user_id]['subscriptions'].add(symbol)

            # Send initial data
            data = await self.get_latest_price_data(symbol)
            if data:
                self.socketio.emit('price_update', data, room=request.sid)
                logger.info(f"User {user_id} subscribed to {symbol} with initial data")
                
                # Start periodic updates
                await self.start_periodic_updates(user_id, symbol)
                return True
            return False
        except Exception as e:
            logger.error(f"Error subscribing to symbol {symbol}: {str(e)}")
            return False

    async def start_periodic_updates(self, user_id, symbol):
        """Start periodic updates for a symbol"""
        try:
            async def update():
                if user_id in self.connected_users and symbol in self.connected_users[user_id]['subscriptions']:
                    data = await self.get_latest_price_data(symbol)
                    if data:
                        self.socketio.emit('price_update', data, room=self.connected_users[user_id]['sid'])
                        logger.debug(f"Sent periodic update for {symbol} to user {user_id}")
            
            # Schedule updates every 10 seconds instead of 5
            self.socketio.start_background_task(update)
        except Exception as e:
            logger.error(f"Error starting periodic updates for {symbol}: {str(e)}")

    def unsubscribe_symbol(self, user_id, symbol):
        """Unsubscribe user from symbol updates"""
        try:
            if user_id in self.connected_users and symbol in self.connected_users[user_id]['subscriptions']:
                self.connected_users[user_id]['subscriptions'].remove(symbol)
                logger.info(f"User {user_id} unsubscribed from {symbol}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error unsubscribing from symbol {symbol}: {str(e)}")
            return False

    async def get_latest_price_data(self, symbol: str) -> Dict:
        """Get latest price data with improved error handling and data validation."""
        try:
            # Check if symbol is OTC
            is_otc = symbol.endswith('_OTC')
            
            # Get real-time price data
            price, source = self.otc_handler.get_realtime_price(symbol, return_source=True)
            if price is None:
                logger.error(f"Failed to get price data for {symbol}")
                return None

            # Get historical data
            historical_data = self.otc_handler.get_historical_data(symbol)
            
            # Prepare basic price data
            price_data = {
                'symbol': symbol,
                'price': price,
                'source': source,
                'timestamp': datetime.now().isoformat(),
                'indicators': {},
                'is_otc': is_otc,
                'status': 'success'
            }
            
            # Add historical data if available
            if historical_data is not None and not historical_data.empty:
                try:
                    # Calculate technical indicators
                    indicators = self.otc_handler.calculate_technical_indicators(historical_data)
                    if indicators:
                        price_data['indicators'] = indicators
                        price_data['historical'] = {
                            'open': float(historical_data['open'].iloc[-1]),
                            'high': float(historical_data['high'].iloc[-1]),
                            'low': float(historical_data['low'].iloc[-1]),
                            'close': float(historical_data['close'].iloc[-1]),
                            'volume': float(historical_data['volume'].iloc[-1])
                        }
                        price_data['status'] = 'success_with_indicators'
                    else:
                        logger.warning(f"Failed to calculate indicators for {symbol}")
                        price_data['status'] = 'success_no_indicators'
                except Exception as e:
                    logger.error(f"Error calculating indicators for {symbol}: {str(e)}")
                    price_data['status'] = 'success_no_indicators'
            else:
                logger.warning(f"No historical data available for {symbol}")
                price_data['status'] = 'success_no_historical'
            
            # Update cache with new data
            self.price_cache[symbol] = price_data
            logger.info(f"Updated price data for {symbol}: {price} from {source} (status: {price_data['status']})")
            
            return price_data
            
        except Exception as e:
            logger.error(f"Error getting latest price data for {symbol}: {str(e)}")
            return None

    def calculate_price_change(self, data):
        """Calculate price change percentage"""
        try:
            if len(data) < 2:
                return 0.0
            return ((data['Close'].iloc[-1] - data['Close'].iloc[0]) / data['Close'].iloc[0]) * 100
        except Exception as e:
            logger.error(f"Error calculating price change: {str(e)}")
            return 0.0

    def calculate_indicators(self, data):
        """Calculate technical indicators"""
        try:
            # RSI
            delta = data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))

            # MACD
            exp1 = data['Close'].ewm(span=12, adjust=False).mean()
            exp2 = data['Close'].ewm(span=26, adjust=False).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9, adjust=False).mean()

            return {
                'rsi': rsi.iloc[-1],
                'macd': macd.iloc[-1],
                'macd_signal': signal.iloc[-1]
            }
        except Exception as e:
            logger.error(f"Error calculating indicators: {str(e)}")
            return {
                'rsi': 50.0,
                'macd': 0.0,
                'macd_signal': 0.0
            }

    async def broadcast_updates(self):
        """Broadcast price updates to all connected clients with improved error handling."""
        while True:
            try:
                if not self.active_connections:
                    await asyncio.sleep(1)
                    continue

                for symbol in self.subscribed_symbols:
                    try:
                        # Add delay between requests to avoid rate limits
                        await asyncio.sleep(0.5)  # 500ms delay between symbols
                        
                        price_data = await self.get_latest_price_data(symbol)
                        if price_data:
                            # Prepare message with additional metadata
                            message = {
                                'type': 'price_update',
                                'data': price_data,
                                'timestamp': datetime.now().isoformat(),
                                'status': price_data.get('status', 'success')
                            }
                            
                            # Broadcast to all clients subscribed to this symbol
                            for connection in self.active_connections:
                                if symbol in self.connection_subscriptions.get(connection, set()):
                                    try:
                                        await connection.send_json(message)
                                    except Exception as e:
                                        logger.error(f"Error sending update to client for {symbol}: {str(e)}")
                                        # Remove failed connection
                                        await self.handle_disconnect(connection)
                        else:
                            logger.warning(f"No price data available for {symbol}")
                    except Exception as e:
                        logger.error(f"Error processing updates for {symbol}: {str(e)}")
                        continue

                await asyncio.sleep(1)  # Wait before next update cycle
            except Exception as e:
                logger.error(f"Error in broadcast loop: {str(e)}")
                await asyncio.sleep(1)  # Wait before retrying

    def update_price(self, symbol: str, price_data: Dict):
        """Update price data and broadcast to subscribed clients"""
        try:
            # Update cache
            self.price_cache[symbol] = {
                'symbol': symbol,
                'price': price_data.get('price'),
                'change': price_data.get('change'),
                'volume': price_data.get('volume'),
                'timestamp': datetime.now().isoformat()
            }
            
            # Broadcast to all subscribed clients
            self.socketio.emit('price_update', self.price_cache[symbol])
            
        except Exception as e:
            logger.error(f'Error updating price: {str(e)}')
            
    def broadcast_trade(self, trade_data: Dict):
        """Broadcast trade information to all clients"""
        try:
            self.socketio.emit('trade_update', trade_data)
        except Exception as e:
            logger.error(f'Error broadcasting trade: {str(e)}')
            
    def broadcast_signal(self, signal_data: Dict):
        """Broadcast trading signal to all clients"""
        try:
            self.socketio.emit('signal_update', signal_data)
        except Exception as e:
            logger.error(f'Error broadcasting signal: {str(e)}')
            
    def broadcast_alert(self, alert_data: Dict):
        """Broadcast alert to all clients"""
        try:
            self.socketio.emit('alert', alert_data)
        except Exception as e:
            logger.error(f'Error broadcasting alert: {str(e)}')
            
    def get_cached_price(self, symbol: str) -> Optional[Dict]:
        """Get cached price data for a symbol"""
        return self.price_cache.get(symbol)
        
    def clear_cache(self, symbol: Optional[str] = None):
        """Clear price cache for a symbol or all symbols"""
        try:
            if symbol:
                self.price_cache.pop(symbol, None)
            else:
                self.price_cache.clear()
        except Exception as e:
            logger.error(f'Error clearing cache: {str(e)}') 