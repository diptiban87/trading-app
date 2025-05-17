import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, send_file, g, flash, jsonify, abort
from datetime import datetime, timedelta
import random
import csv
import io
import requests
from werkzeug.security import generate_password_hash, check_password_hash
import math
from scipy.stats import norm
import time
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
import pandas as pd
import numpy as np
import yfinance as yf
from flask_socketio import SocketIO, emit
from otc_data import OTCDataHandler
from trading_system import TradingSystem
from risk_manager import RiskManager
from auto_trader import AutoTrader
from websocket_handler import WebSocketHandler
from config import (
    ALPHA_VANTAGE_API_KEY,
    API_TIMEOUT,
    CACHE_DURATION,
    PREMIUM_API_ENABLED,
    PREMIUM_API_CALLS_PER_MINUTE,
    MAX_REQUESTS_PER_MINUTE,
    LOG_LEVEL
)
import logging
from typing import Dict, Tuple, Optional, Union
import json
import threading
import asyncio
from functools import wraps
import redis
from ratelimit import limits, sleep_and_retry
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

# Initialize Sentry for error tracking
sentry_sdk.init(
    dsn="YOUR_SENTRY_DSN",
    integrations=[FlaskIntegration()],
    traces_sample_rate=1.0,
    environment="production"
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Redis for caching
redis_client = redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
)

# Rate limiting decorator
def rate_limit(limit: int, period: int = 60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key = f"rate_limit:{request.remote_addr}:{f.__name__}"
            current = redis_client.get(key)
            if current and int(current) >= limit:
                return jsonify({"error": "Rate limit exceeded"}), 429
            pipe = redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, period)
            pipe.execute()
            return f(*args, **kwargs)
        return wrapped
    return decorator

# Data validation decorator
def validate_input(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            # Validate pair format
            if 'pair' in request.form:
                pair = request.form['pair']
                if not isinstance(pair, str) or len(pair) < 6:
                    return jsonify({"error": "Invalid pair format"}), 400
                
            # Validate time format
            if all(x in request.form for x in ['start_hour', 'start_minute', 'end_hour', 'end_minute']):
                try:
                    start = datetime.strptime(
                        f"{request.form['start_hour']}:{request.form['start_minute']}", 
                        "%H:%M"
                    )
                    end = datetime.strptime(
                        f"{request.form['end_hour']}:{request.form['end_minute']}", 
                        "%H:%M"
                    )
                    if start >= end:
                        return jsonify({"error": "Start time must be before end time"}), 400
                except ValueError:
                    return jsonify({"error": "Invalid time format"}), 400

            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Input validation error: {str(e)}")
            return jsonify({"error": "Invalid input"}), 400
    return wrapped

app = Flask(__name__)
app.secret_key = "kishan_secret"
socketio = SocketIO(app)
DATABASE = os.path.join(app.root_path, 'kishanx.db')

# Initialize components
trading_system = TradingSystem()
risk_manager = RiskManager()
auto_trader = AutoTrader(trading_system, risk_manager)
websocket_handler = WebSocketHandler(socketio)

# Initialize database on startup
with app.app_context():
    init_db()

# --- User helpers ---
def get_user_by_username(username):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

def get_user_by_id(user_id):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

def create_user(username, password):
    db = get_db()
    hashed = generate_password_hash(password)
    db.execute('INSERT INTO users (username, password, registered_at) VALUES (?, ?, ?)',
               (username, hashed, datetime.now().isoformat()))
    db.commit()

def update_last_login(user_id):
    db = get_db()
    db.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now().isoformat(), user_id))
    db.commit()

def verify_user(username, password):
    user = get_user_by_username(username)
    if user and check_password_hash(user['password'], password):
        return user
    return None

# --- Signal helpers ---
def save_signal(user_id, time, pair, direction):
    db = get_db()
    db.execute('INSERT INTO signals (user_id, time, pair, direction, created_at) VALUES (?, ?, ?, ?, ?)',
               (user_id, time, pair, direction, datetime.now().isoformat()))
    db.commit()

def get_signals_for_user(user_id, limit=20):
    db = get_db()
    return db.execute('SELECT * FROM signals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', (user_id, limit)).fetchall()

def get_signal_stats(user_id):
    db = get_db()
    total = db.execute('SELECT COUNT(*) FROM signals WHERE user_id = ?', (user_id,)).fetchone()[0]
    by_pair = db.execute('SELECT pair, COUNT(*) as count FROM signals WHERE user_id = ? GROUP BY pair', (user_id,)).fetchall()
    by_direction = db.execute('SELECT direction, COUNT(*) as count FROM signals WHERE user_id = ? GROUP BY direction', (user_id,)).fetchall()
    return total, by_pair, by_direction

# --- App logic ---
pairs = ["EURAUD", "USDCHF", "USDBRL", "AUDUSD", "GBPCAD", "EURCAD", "NZDUSD", "USDPKR", "EURUSD", "USDCAD", "AUDCHF", "GBPUSD", "EURGBP"]
brokers = ["Quotex", "Pocket Option", "Binolla", "IQ Option", "Bullex", "Exnova"]

# Initialize price cache
price_cache = {}

# Symbol mapping for Indian markets
symbol_map = {
    # Major Indices
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NSEBANK": "^NSEBANK",
    "NSEIT": "^CNXIT",
    "NSEINFRA": "^CNXINFRA",
    "NSEPHARMA": "^CNXPHARMA",
    "NSEFMCG": "^CNXFMCG",
    "NSEMETAL": "^CNXMETAL",
    "NSEENERGY": "^CNXENERGY",
    "NSEAUTO": "^CNXAUTO",
    # Additional Indices
    "NIFTYMIDCAP": "^NSEI_MIDCAP",
    "NIFTYSMALLCAP": "^NSEI_SMALLCAP",
    "NIFTYNEXT50": "^NSEI_NEXT50",
    "NIFTY100": "^NSEI_100",
    "NIFTY500": "^NSEI_500",
    # Sector Indices
    "NIFTYREALTY": "^NSEI_REALTY",
    "NIFTYPVTBANK": "^NSEI_PVTBANK",
    "NIFTYPSUBANK": "^NSEI_PSUBANK",
    "NIFTYFIN": "^NSEI_FIN",
    "NIFTYMEDIA": "^NSEI_MEDIA",
    # Popular Stocks
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "SBIN": "SBIN.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "BAJFINANCE": "BAJFINANCE.NS"
}

broker_payouts = {
    "Quotex": 0.85,
    "Pocket Option": 0.80,
    "Binolla": 0.78,
    "IQ Option": 0.82,
    "Bullex": 0.75,
    "Exnova": 0.77
}

def get_cached_realtime_forex(pair, api_key=ALPHA_VANTAGE_API_KEY, cache_duration=CACHE_DURATION, return_source=False):
    """
    Get cached real-time forex rate with improved error handling for premium API.
    
    Args:
        pair (str): Currency pair (e.g., 'EURUSD')
        api_key (str): Alpha Vantage API key
        cache_duration (int): Cache duration in seconds
    
    Returns:
        float: Current exchange rate or None if unavailable
    """
    cache_key = f"forex_{pair}"
    now = time.time()
    
    # Check cache
    if cache_key in price_cache:
        price, timestamp = price_cache[cache_key]
        if now - timestamp < cache_duration:
            logger.info(f"Using cached price for {pair}: {price} (cached {int(now - timestamp)} seconds ago)")
            if return_source:
                return price, 'cached'
            return price
        else:
            logger.info(f"Cache expired for {pair}, fetching new data...")
    
    try:
        price = get_realtime_forex(pair, api_key)
        if price is not None:
            price_cache[cache_key] = (price, now)
            logger.info(f"Updated cache for {pair} with new price: {price}")
            if return_source:
                return price, 'cached'
            return price
    except Exception as e:
        logger.error(f"Error getting forex rate for {pair}: {str(e)}")
        return None

def get_realtime_forex(pair, api_key=ALPHA_VANTAGE_API_KEY):
    """
    Get real-time forex rate with support for premium API features.
    
    Args:
        pair (str): Currency pair (e.g., 'EURUSD')
        api_key (str): Alpha Vantage API key
    
    Returns:
        float: Current exchange rate or None if unavailable
    """
    if not api_key or api_key == "YOUR_PREMIUM_API_KEY":
        logger.warning("Using fallback data - No valid API key provided")
        return None
        
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    
    # Try multiple data sources in order of preference
    data_sources = [
        lambda: _get_alpha_vantage_rate(pair, api_key),
        lambda: _get_exchange_rate_api_rate(pair),
        lambda: _get_fixer_io_rate(pair)
    ]
    
    for source in data_sources:
        try:
            rate = source()
            if rate is not None:
                return rate
        except Exception as e:
            logger.error(f"Error with data source: {str(e)}")
            continue
    
    return None

def _get_alpha_vantage_rate(pair, api_key):
    """Get rate from Alpha Vantage with premium API support."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    
    url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={from_symbol}&to_currency={to_symbol}&apikey={api_key}"
    
    try:
        logger.info(f"Fetching rate for {pair} from Alpha Vantage")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if "Error Message" in data:
            logger.error(f"Alpha Vantage API Error: {data['Error Message']}")
            return None
            
        if "Note" in data:
            if "premium" in data["Note"].lower():
                if not PREMIUM_API_ENABLED:
                    logger.warning("Premium API features required. Set PREMIUM_API_ENABLED=True in config.py")
                return None
            elif "API call frequency" in data["Note"]:
                logger.warning(f"API rate limit reached: {data['Note']}")
                return None
            
        if "Realtime Currency Exchange Rate" in data:
            rate = float(data["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except requests.exceptions.Timeout:
        logger.error(f"Timeout while fetching rate for {pair}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error while fetching rate for {pair}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error while fetching rate for {pair}: {str(e)}")
        return None
    
    return None

def _get_exchange_rate_api_rate(pair):
    """Get rate from ExchangeRate-API with error handling."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"https://open.er-api.com/v6/latest/{from_symbol}"
    
    try:
        logger.info(f"Fetching rate for {pair} from ExchangeRate-API")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if data.get("result") == "error":
            logger.error(f"ExchangeRate-API Error: {data.get('error-type', 'Unknown error')}")
            return None
            
        if data.get("rates") and to_symbol in data["rates"]:
            rate = float(data["rates"][to_symbol])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except Exception as e:
        logger.error(f"Error fetching from ExchangeRate-API for {pair}: {str(e)}")
        return None
    
    return None

def _get_fixer_io_rate(pair):
    """Get rate from Fixer.io with error handling."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"http://data.fixer.io/api/latest?access_key=YOUR_FIXER_API_KEY&base={from_symbol}&symbols={to_symbol}"
    
    try:
        logger.info(f"Fetching rate for {pair} from Fixer.io")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if not data.get("success", False):
            logger.error(f"Fixer.io API Error: {data.get('error', {}).get('info', 'Unknown error')}")
            return None
            
        if data.get("rates") and to_symbol in data["rates"]:
            rate = float(data["rates"][to_symbol])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except Exception as e:
        logger.error(f"Error fetching from Fixer.io for {pair}: {str(e)}")
        return None
    
    return None

def black_scholes_call_put(S, K, T, r, sigma, option_type="call"):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return price

DEMO_UNLOCK_PASSWORD = 'Indiandemo2021'
DEMO_TIMEOUT_MINUTES = 30

@app.before_request
def demo_lockout():
    allowed_routes = {'login', 'register', 'static', 'lock', 'unlock'}
    if request.endpoint in allowed_routes or request.endpoint is None:
        return
    if 'demo_start_time' not in session:
        session['demo_start_time'] = datetime.now().isoformat()
    start_time = datetime.fromisoformat(session['demo_start_time'])
    if (datetime.now() - start_time).total_seconds() > DEMO_TIMEOUT_MINUTES * 60:
        session['locked'] = True
        if request.endpoint not in {'lock', 'unlock'}:
            return redirect(url_for('lock'))
    else:
        session['locked'] = False

@app.route('/lock', methods=['GET'])
def lock():
    return render_template('lock.html')

@app.route('/unlock', methods=['POST'])
def unlock():
    password = request.form.get('password')
    if password == DEMO_UNLOCK_PASSWORD:
        session['demo_start_time'] = datetime.now().isoformat()
        session['locked'] = False
        return redirect(url_for('dashboard'))
    else:
        flash('Incorrect password. Please try again.', 'error')
        return render_template('lock.html')

@app.route('/get_demo_time')
def get_demo_time():
    demo_timeout = DEMO_TIMEOUT_MINUTES
    start_time = session.get('demo_start_time')
    if not start_time:
        # fallback: reset timer
        session['demo_start_time'] = datetime.now().isoformat()
        start_time = session['demo_start_time']
    start_time = datetime.fromisoformat(start_time)
    elapsed = (datetime.now() - start_time).total_seconds()
    remaining = max(0, int(demo_timeout * 60 - elapsed))
    minutes = remaining // 60
    seconds = remaining % 60
    time_left = f"{minutes:02d}:{seconds:02d}"
    return jsonify({'time_left': time_left})

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if get_user_by_username(username):
            flash("Username already exists.", "error")
            return render_template("register.html")
        create_user(username, password)
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = verify_user(username, password)
        if user:
            session["user_id"] = user["id"]
            update_last_login(user["id"])
            # Get the next page from the request args, default to dashboard
            next_page = request.args.get('next', url_for('dashboard'))
            return redirect(next_page)
        else:
            return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = get_user_by_id(session["user_id"])
    if request.method == "POST":
        new_password = request.form["new_password"]
        if new_password:
            db = get_db()
            db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(new_password), user["id"]))
            db.commit()
            flash("Password updated successfully.", "success")
    return render_template("profile.html", user=user)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = get_user_by_id(session["user_id"])
    signals = get_signals_for_user(user["id"], limit=10)
    total, by_pair, by_direction = get_signal_stats(user["id"])
    pair_labels = [p['pair'] for p in by_pair]
    pair_counts = [p['count'] for p in by_pair]
    direction_labels = [d['direction'] for d in by_direction]
    direction_counts = [d['count'] for d in by_direction]
    return render_template(
        "dashboard.html",
        user=user,
        signals=signals,
        total=total,
        pair_labels=pair_labels,
        pair_counts=pair_counts,
        direction_labels=direction_labels,
        direction_counts=direction_counts
    )

@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    current_rate = None
    selected_pair = pairs[0]
    selected_broker = brokers[0]
    payout = broker_payouts[selected_broker]
    call_price = None
    put_price = None
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    if request.method == "POST":
        pair = request.form["pair"]
        broker = request.form["broker"]
        signal_type = request.form["signal_type"].upper()
        start_hour = request.form["start_hour"]
        start_minute = request.form["start_minute"]
        end_hour = request.form["end_hour"]
        end_minute = request.form["end_minute"]
        start_str = f"{start_hour}:{start_minute}"
        end_str = f"{end_hour}:{end_minute}"
        selected_pair = pair
        selected_broker = broker
        payout = broker_payouts.get(broker, 0.75)
        current_rate = get_cached_realtime_forex(pair)
        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("index.html", error="Start time must be before end time.", pairs=pairs, brokers=brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

            signals = []
            current = start
            while current < end:
                direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["signals"] = signals
            return render_template("results.html", signals=signals, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)
        except ValueError:
            return render_template("index.html", error="Invalid time format.", pairs=pairs, brokers=brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

    # For GET requests, show the rate for the default pair and broker
    current_rate = get_cached_realtime_forex(selected_pair)
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
    return render_template("index.html", pairs=pairs, brokers=brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

@app.route("/download")
def download():
    if "signals" not in session:
        return redirect(url_for("index"))

    signals = session["signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    # Table header
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        S = get_cached_realtime_forex(s["pair"])
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    # Draw table
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_signals.pdf")

# Initialize OTC data handler
otc_handler = OTCDataHandler(ALPHA_VANTAGE_API_KEY)

@app.route('/api/price/<symbol>')
def api_price(symbol):
    """Get real-time price for a symbol"""
    try:
        # Get price from OTC data handler
        price_data = otc_handler.get_realtime_price(symbol)
        
        if price_data is None:
            logger.error(f"Failed to get price data for {symbol}")
            return jsonify({
                'error': 'Failed to get real-time price data',
                'symbol': symbol,
                'source': 'None'
            }), 404
            
        # If price_data is a tuple (price, source), extract both values
        if isinstance(price_data, tuple):
            price = price_data[0]
            source = price_data[1]
        else:
            price = price_data
            source = 'Unknown'
            
        # Add spread for OTC pairs
        spread = 0.0002  # 2 pips spread
        bid = price * (1 - spread)
        ask = price * (1 + spread)
        
        # Calculate option prices using Black-Scholes
        S = price  # Current price
        K = price  # Strike price (same as current price)
        T = 1/365  # Time to expiry (1 day)
        r = 0.01   # Risk-free rate
        sigma = 0.2 # Volatility
        
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
        
        logger.info(f"Successfully got price for {symbol}: bid={bid}, ask={ask} from {source}")
        
        return jsonify({
            'symbol': symbol,
            'bid': round(bid, 5),
            'ask': round(ask, 5),
            'rate': round(price, 5),
            'source': source,
            'call_price': round(call_price, 6) if call_price else None,
            'put_price': round(put_price, 6) if put_price else None,
            'volatility': sigma,
            'expiry': T,
            'risk_free_rate': r,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error in api_price for {symbol}: {str(e)}")
        return jsonify({
            'error': 'Internal server error',
            'message': str(e),
            'symbol': symbol,
            'source': 'Error'
        }), 500

# --- Indian Market Data ---
indian_pairs = [
    # Major Indices
    "NIFTY50", "BANKNIFTY", "NSEBANK", "NSEIT", "NSEINFRA", "NSEPHARMA", "NSEFMCG", "NSEMETAL", "NSEENERGY", "NSEAUTO",
    # Additional Indices
    "NIFTYMIDCAP", "NIFTYSMALLCAP", "NIFTYNEXT50", "NIFTY100", "NIFTY500",
    # Sector Indices
    "NIFTYREALTY", "NIFTYPVTBANK", "NIFTYPSUBANK", "NIFTYFIN", "NIFTYMEDIA",
    # Popular Stocks
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "SBIN", "BHARTIARTL", "KOTAKBANK", "BAJFINANCE"
]
indian_brokers = ["Zerodha", "Upstox", "Angel One", "Groww", "ICICI Direct", "HDFC Securities"]

@app.route("/indian", methods=["GET", "POST"])
def indian_market():
    if "user_id" not in session:
        return redirect(url_for("login"))

    current_rate = None
    selected_pair = indian_pairs[0]
    selected_broker = indian_brokers[0]
    payout = 0.75  # Indian brokers may not have payout, but keep for UI consistency
    call_price = None
    put_price = None
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    if request.method == "POST":
        pair = request.form["pair"]
        broker = request.form["broker"]
        signal_type = request.form["signal_type"].upper()
        start_hour = request.form["start_hour"]
        start_minute = request.form["start_minute"]
        end_hour = request.form["end_hour"]
        end_minute = request.form["end_minute"]
        start_str = f"{start_hour}:{start_minute}"
        end_str = f"{end_hour}:{end_minute}"
        selected_pair = pair
        selected_broker = broker
        # For demo, use get_cached_realtime_forex with a fallback for Indian symbols
        try:
            current_rate = get_cached_realtime_forex(pair)
        except Exception:
            current_rate = round(random.uniform(10000, 50000), 2)
        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("indian.html", error="Start time must be before end time.", pairs=indian_pairs, brokers=indian_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

            signals = []
            current = start
            while current < end:
                direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["indian_signals"] = signals
            return render_template("indian.html", signals=signals, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate, pairs=indian_pairs, brokers=indian_brokers)
        except ValueError:
            return render_template("indian.html", error="Invalid time format.", pairs=indian_pairs, brokers=indian_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate)

    # For GET requests, show the rate for the default pair and broker
    try:
        current_rate = get_cached_realtime_forex(selected_pair)
    except Exception:
        current_rate = round(random.uniform(10000, 50000), 2)
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
    signals = session.get("indian_signals", [])
    return render_template("indian.html", pairs=indian_pairs, brokers=indian_brokers, current_rate=current_rate, selected_pair=selected_pair, selected_broker=selected_broker, payout=payout, call_price=call_price, put_price=put_price, volatility=volatility, expiry=expiry, risk_free_rate=risk_free_rate, signals=signals)

# --- OTC Market Data ---
otc_pairs = [
    # Major OTC Pairs
    "AUDCAD_OTC", "AUDCHF_OTC", "AUDHKD_OTC", "AUDJPY_OTC", "AUDMXN_OTC", "AUDNZD_OTC", "AUDSGD_OTC", "AUDUSD_OTC", "AUDZAR_OTC",
    "EURCAD_OTC", "EURCHF_OTC", "EURGBP_OTC", "EURHKD_OTC", "EURJPY_OTC", "EURMXN_OTC", "EURNZD_OTC", "EURSGD_OTC", "EURUSD_OTC", "EURZAR_OTC",
    "GBPCAD_OTC", "GBPCHF_OTC", "GBPHKD_OTC", "GBPJPY_OTC", "GBPMXN_OTC", "GBPNZD_OTC", "GBPSGD_OTC", "GBPUSD_OTC", "GBPZAR_OTC",
    "NZDCHF_OTC", "NZDUSD_OTC",
    "USDCAD_OTC", "USDCHF_OTC", "USDHKD_OTC", "USDJPY_OTC", "USDMXN_OTC", "USDNZD_OTC", "USDSGD_OTC", "USDZAR_OTC",
    # Additional Exotic OTC Pairs
    "USDARS_OTC", "USDBRL_OTC", "USDPKR_OTC"
]
otc_brokers = ["Quotex", "Pocket Option", "Binolla", "IQ Option", "Bullex", "Exnova"]

@app.route("/otc", methods=["GET", "POST"])
@rate_limit(MAX_REQUESTS_PER_MINUTE)
@validate_input
def otc_market():
    """
    OTC market route with enhanced error handling and validation.
    """
    try:
        if "user_id" not in session:
            return redirect(url_for("login"))

        # Initialize variables with type hints
        current_rate: Optional[float] = None
        selected_pair: str = otc_pairs[0]
        selected_broker: str = otc_brokers[0]
        payout: float = broker_payouts[selected_broker]
        call_price: Optional[float] = None
        put_price: Optional[float] = None
        volatility: float = 0.2
        expiry: float = 1/365
        risk_free_rate: float = 0.01
        data_source: Optional[str] = None

        if request.method == "POST":
            try:
                # Extract and validate form data
                pair = request.form["pair"]
                broker = request.form["broker"]
                signal_type = request.form["signal_type"].upper()
                
                # Validate pair and broker
                if pair not in otc_pairs:
                    return jsonify({"error": "Invalid pair"}), 400
                if broker not in otc_brokers:
                    return jsonify({"error": "Invalid broker"}), 400
                
                # Get real-time price with error handling
                try:
                    price_data = otc_handler.get_realtime_price(pair)
                    if isinstance(price_data, tuple):
                        current_rate, data_source = price_data
                    else:
                        current_rate = price_data
                        data_source = "Unknown"
                except Exception as e:
                    logger.error(f"Error getting real-time price: {str(e)}")
                    return jsonify({"error": "Failed to get price data"}), 500

                # Calculate option prices if rate is available
                if current_rate:
                    try:
                        S = current_rate
                        K = S
                        T = expiry
                        r = risk_free_rate
                        sigma = volatility
                        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
                        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
                    except Exception as e:
                        logger.error(f"Error calculating option prices: {str(e)}")
                        call_price = put_price = None

                # Get historical data with error handling
                try:
                    historical_data = otc_handler.get_historical_data(pair, interval='1min')
                    if historical_data is not None:
                        indicators = calculate_technical_indicators(historical_data)
                        if indicators:
                            session['otc_indicators'] = indicators
                        else:
                            logger.warning("Failed to calculate indicators")
                except Exception as e:
                    logger.error(f"Error getting historical data: {str(e)}")

                # Generate signals with enhanced error handling
                signals = []
                current = datetime.strptime(f"{request.form['start_hour']}:{request.form['start_minute']}", "%H:%M")
                end = datetime.strptime(f"{request.form['end_hour']}:{request.form['end_minute']}", "%H:%M")

                while current < end:
                    try:
                        signal = generate_signal(
                            indicators=session.get('otc_indicators'),
                            current_rate=current_rate,
                            signal_type=signal_type
                        )
                        signals.append(signal)
                        save_signal(session["user_id"], current.strftime("%H:%M"), pair, signal["direction"])
                        current += timedelta(minutes=random.randint(1, 15))
                    except Exception as e:
                        logger.error(f"Error generating signal: {str(e)}")
                        continue

                session["otc_signals"] = signals
                
                return render_template("otc.html",
                    signals=signals,
                    current_rate=current_rate,
                    selected_pair=selected_pair,
                    selected_broker=selected_broker,
                    payout=payout,
                    call_price=call_price,
                    put_price=put_price,
                    volatility=volatility,
                    expiry=expiry,
                    risk_free_rate=risk_free_rate,
                    pairs=otc_pairs,
                    brokers=otc_brokers,
                    data_source=data_source
                )

            except Exception as e:
                logger.error(f"Error processing POST request: {str(e)}")
                return jsonify({"error": "Internal server error"}), 500

        # Handle GET request
        try:
            price_data = otc_handler.get_realtime_price(selected_pair)
            if isinstance(price_data, tuple):
                current_rate, data_source = price_data
            else:
                current_rate = price_data
                data_source = "Unknown"
        except Exception as e:
            logger.error(f"Error getting price data: {str(e)}")
            current_rate = None
            data_source = "Error"

        if current_rate:
            try:
                S = current_rate
                K = S
                T = expiry
                r = risk_free_rate
                sigma = volatility
                call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
                put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
            except Exception as e:
                logger.error(f"Error calculating option prices: {str(e)}")
                call_price = put_price = None

        signals = session.get("otc_signals", [])
        return render_template("otc.html",
            pairs=otc_pairs,
            brokers=otc_brokers,
            current_rate=current_rate,
            selected_pair=selected_pair,
            selected_broker=selected_broker,
            payout=payout,
            call_price=call_price,
            put_price=put_price,
            volatility=volatility,
            expiry=expiry,
            risk_free_rate=risk_free_rate,
            signals=signals,
            data_source=data_source
        )

    except Exception as e:
        logger.error(f"Unexpected error in otc_market: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

def generate_signal(
    indicators: Optional[Dict],
    current_rate: Optional[float],
    signal_type: str
) -> Dict:
    """
    Generate trading signal with enhanced error handling and validation.
    
    Args:
        indicators (Optional[Dict]): Technical indicators
        current_rate (Optional[float]): Current price rate
        signal_type (str): Type of signal to generate
        
    Returns:
        Dict: Generated signal with confidence and indicators
    """
    try:
        signal_confidence = 0
        direction = None

        if indicators and current_rate:
            # Extract and validate indicator values
            rsi = indicators.get('rsi', 50)
            macd = indicators.get('macd', 0)
            macd_signal = indicators.get('macd_signal', 0)
            sma = indicators.get('sma', [])
            ema = indicators.get('ema', [])
            bb_upper = indicators.get('bollinger_upper', [])
            bb_lower = indicators.get('bollinger_lower', [])
            cci = indicators.get('cci', 0)
            volume_ratio = indicators.get('volume_ratio', 1)

            # Convert to single values if they are arrays
            if isinstance(rsi, (list, np.ndarray)):
                rsi = rsi[-1] if len(rsi) > 0 else 50
            if isinstance(macd, (list, np.ndarray)):
                macd = macd[-1] if len(macd) > 0 else 0
            if isinstance(macd_signal, (list, np.ndarray)):
                macd_signal = macd_signal[-1] if len(macd_signal) > 0 else 0
            if isinstance(sma, (list, np.ndarray)):
                sma = sma[-1] if len(sma) > 0 else current_rate
            if isinstance(ema, (list, np.ndarray)):
                ema = ema[-1] if len(ema) > 0 else current_rate
            if isinstance(bb_upper, (list, np.ndarray)):
                bb_upper = bb_upper[-1] if len(bb_upper) > 0 else current_rate * 1.02
            if isinstance(bb_lower, (list, np.ndarray)):
                bb_lower = bb_lower[-1] if len(bb_lower) > 0 else current_rate * 0.98
            if isinstance(cci, (list, np.ndarray)):
                cci = cci[-1] if len(cci) > 0 else 0
            if isinstance(volume_ratio, (list, np.ndarray)):
                volume_ratio = volume_ratio[-1] if len(volume_ratio) > 0 else 1

            # Validate indicator values
            if not all(isinstance(x, (int, float)) for x in [rsi, macd, macd_signal, sma, ema, bb_upper, bb_lower, cci, volume_ratio]):
                logger.warning("Invalid indicator values detected")
                return {
                    "time": datetime.now().strftime("%H:%M"),
                    "direction": signal_type if signal_type != "BOTH" else random.choice(["CALL", "PUT"]),
                    "confidence": 0,
                    "indicators": None
                }

            # Calculate signal confidence
            try:
                # RSI Analysis
                if rsi > 70:
                    signal_confidence -= 1
                elif rsi < 30:
                    signal_confidence += 1

                # MACD Analysis
                if macd > macd_signal:
                    signal_confidence += 1
                else:
                    signal_confidence -= 1

                # Moving Average Analysis
                if ema > sma:
                    signal_confidence += 1
                else:
                    signal_confidence -= 1

                # Bollinger Bands Analysis
                if current_rate > bb_upper:
                    signal_confidence -= 1
                elif current_rate < bb_lower:
                    signal_confidence += 1

                # CCI Analysis
                if cci > 100:
                    signal_confidence -= 1
                elif cci < -100:
                    signal_confidence += 1

                # Volume Analysis
                if volume_ratio > 1.5:  # High volume
                    if signal_confidence > 0:
                        signal_confidence += 1
                    elif signal_confidence < 0:
                        signal_confidence -= 1
                elif volume_ratio < 0.5:  # Low volume
                    signal_confidence = signal_confidence * 0.5

            except Exception as e:
                logger.error(f"Error calculating signal confidence: {str(e)}")
                signal_confidence = 0

            # Determine direction based on confidence
            if signal_confidence >= 2:
                direction = "CALL"
            elif signal_confidence <= -2:
                direction = "PUT"
            else:
                direction = signal_type if signal_type != "BOTH" else random.choice(["CALL", "PUT"])

            return {
                "time": datetime.now().strftime("%H:%M"),
                "direction": direction,
                "confidence": abs(signal_confidence),
                "indicators": {
                    "rsi": round(rsi, 2),
                    "macd": round(macd, 5),
                    "macd_signal": round(macd_signal, 5),
                    "sma": round(sma, 5),
                    "ema": round(ema, 5),
                    "bb_upper": round(bb_upper, 5),
                    "bb_lower": round(bb_lower, 5),
                    "cci": round(cci, 2),
                    "volume_ratio": round(volume_ratio, 2)
                }
            }

        # Fallback for missing data
        return {
            "time": datetime.now().strftime("%H:%M"),
            "direction": signal_type if signal_type != "BOTH" else random.choice(["CALL", "PUT"]),
            "confidence": 0,
            "indicators": None
        }

    except Exception as e:
        logger.error(f"Error in generate_signal: {str(e)}")
        return {
            "time": datetime.now().strftime("%H:%M"),
            "direction": signal_type if signal_type != "BOTH" else random.choice(["CALL", "PUT"]),
            "confidence": 0,
            "indicators": None
        }

@app.route("/download_otc")
def download_otc():
    if "otc_signals" not in session:
        return redirect(url_for("otc_market"))
    signals = session["otc_signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX OTC Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        S = get_cached_realtime_forex(s["pair"].replace('_OTC',''))
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_otc_signals.pdf")

@app.route("/download_indian")
def download_indian():
    if "indian_signals" not in session:
        return redirect(url_for("indian_market"))
    signals = session["indian_signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX Indian Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        try:
            S = get_cached_realtime_forex(s["pair"])
        except Exception:
            S = round(random.uniform(10000, 50000), 2)
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_indian_signals.pdf")

def calculate_technical_indicators(data: pd.DataFrame) -> Optional[Dict]:
    """
    Calculate technical indicators with error handling and validation.
    
    Args:
        data (pd.DataFrame): Price data with OHLCV columns
        
    Returns:
        Optional[Dict]: Dictionary of calculated indicators or None if error
    """
    try:
        # Validate input data
        required_columns = ['open', 'high', 'low', 'close', 'volume']
        if not all(col in data.columns for col in required_columns):
            logger.error("Missing required columns in data")
            return None
            
        # Check for sufficient data points
        if len(data) < 20:
            logger.warning("Insufficient data points for indicator calculation")
            return None

        # Calculate indicators with error handling
        try:
            # Moving Averages
            data['SMA_20'] = data['close'].rolling(window=20).mean()
            data['EMA_20'] = data['close'].ewm(span=20, adjust=False).mean()
            
            # MACD
            exp1 = data['close'].ewm(span=12, adjust=False).mean()
            exp2 = data['close'].ewm(span=26, adjust=False).mean()
            data['MACD'] = exp1 - exp2
            data['Signal'] = data['MACD'].ewm(span=9, adjust=False).mean()
            
            # RSI
            delta = data['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            data['RSI'] = 100 - (100 / (1 + rs))
            
            # Bollinger Bands
            data['BB_middle'] = data['close'].rolling(window=20).mean()
            data['BB_std'] = data['close'].rolling(window=20).std()
            data['BB_upper'] = data['BB_middle'] + (data['BB_std'] * 2)
            data['BB_lower'] = data['BB_middle'] - (data['BB_std'] * 2)
            
            # CCI
            tp = (data['high'] + data['low'] + data['close']) / 3
            data['CCI'] = (tp - tp.rolling(window=20).mean()) / (0.015 * tp.rolling(window=20).std())
            
            # Volume Analysis
            data['Volume_SMA'] = data['volume'].rolling(window=20).mean()
            data['Volume_Ratio'] = data['volume'] / data['Volume_SMA']
            
            # Validate calculated values
            for col in data.columns:
                if data[col].isnull().any():
                    logger.warning(f"Null values found in {col}")
                    data[col] = data[col].fillna(method='ffill')
            
            return {
                'sma': data['SMA_20'].tolist(),
                'ema': data['EMA_20'].tolist(),
                'macd': data['MACD'].tolist(),
                'macd_signal': data['Signal'].tolist(),
                'rsi': data['RSI'].tolist(),
                'bollinger_upper': data['BB_upper'].tolist(),
                'bollinger_lower': data['BB_lower'].tolist(),
                'cci': data['CCI'].tolist(),
                'volume_ratio': data['Volume_Ratio'].tolist()
            }
            
        except Exception as e:
            logger.error(f"Error calculating indicators: {str(e)}")
            return None
            
    except Exception as e:
        logger.error(f"Error in calculate_technical_indicators: {str(e)}")
        return None

def get_historical_data(symbol, period='1mo', interval='1d'):
    """Fetch historical market data and calculate technical indicators"""
    try:
        yahoo_symbol = symbol_map.get(symbol)
        if not yahoo_symbol:
            print(f"Invalid symbol: {symbol}")
            return {
                'historical': None,
                'realtime': None,
                'error': f"Invalid symbol: {symbol}"
            }
        
        print(f"Fetching data for {symbol} using Yahoo symbol {yahoo_symbol}")
        
        # Fetch data from Yahoo Finance
        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period, interval=interval)
        
        if df.empty:
            print(f"No data received from Yahoo Finance for {symbol}")
            return {
                'historical': None,
                'realtime': None,
                'error': f"No data available for {symbol}"
            }
        
        # Calculate technical indicators
        # Simple Moving Averages
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        
        # Exponential Moving Averages
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        
        # MACD
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        # RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Bollinger Bands
        df['BB_middle'] = df['Close'].rolling(window=20).mean()
        df['BB_std'] = df['Close'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + (df['BB_std'] * 2)
        df['BB_lower'] = df['BB_middle'] - (df['BB_std'] * 2)
        
        # Replace NaN values with None for JSON serialization
        df = df.replace({np.nan: None})
        
        # Prepare the response data
        dates = df.index.strftime('%Y-%m-%d').tolist()
        
        historical_data = {
            'dates': dates,
            'prices': {
                'open': [round(x, 2) if x is not None else None for x in df['Open'].tolist()],
                'high': [round(x, 2) if x is not None else None for x in df['High'].tolist()],
                'low': [round(x, 2) if x is not None else None for x in df['Low'].tolist()],
                'close': [round(x, 2) if x is not None else None for x in df['Close'].tolist()],
                'volume': [int(x) if x is not None else None for x in df['Volume'].tolist()]
            },
            'indicators': {
                'sma': [round(x, 2) if x is not None else None for x in df['SMA20'].tolist()],
                'ema': [round(x, 2) if x is not None else None for x in df['EMA20'].tolist()],
                'macd': [round(x, 2) if x is not None else None for x in df['MACD'].tolist()],
                'macd_signal': [round(x, 2) if x is not None else None for x in df['Signal'].tolist()],
                'rsi': [round(x, 2) if x is not None else None for x in df['RSI'].tolist()],
                'bollinger_upper': [round(x, 2) if x is not None else None for x in df['BB_upper'].tolist()],
                'bollinger_middle': [round(x, 2) if x is not None else None for x in df['BB_middle'].tolist()],
                'bollinger_lower': [round(x, 2) if x is not None else None for x in df['BB_lower'].tolist()]
            }
        }
        
        # Get real-time data for current values
        realtime_data = get_indian_market_data(symbol)
        
        return {
            'historical': historical_data,
            'realtime': realtime_data
        }
        
    except Exception as e:
        print(f"Error in get_historical_data for {symbol}: {str(e)}")
        return {
            'historical': None,
            'realtime': None,
            'error': str(e)
        }

@app.route("/market_data/<symbol>")
def market_data(symbol):
    """API endpoint to get market data for a symbol"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        timeframe = request.args.get('timeframe', '1mo')
        print(f"Fetching data for {symbol} with timeframe {timeframe}")  # Debug log
        
        data = get_historical_data(symbol, period=timeframe)
        print(f"Received data: {data}")  # Debug log
        
        if not data:
            return jsonify({'error': 'No data available'}), 404
            
        if data.get('error'):
            return jsonify({'error': data['error']}), 500
            
        if not data.get('historical') or not data.get('realtime'):
            return jsonify({'error': 'Incomplete data received'}), 500
            
        return jsonify(data)
        
    except Exception as e:
        print(f"Error in market_data endpoint: {str(e)}")  # Debug log
        return jsonify({'error': str(e)}), 500

def get_trading_signals(symbol: str) -> Dict:
    """Get trading signals for a symbol"""
    try:
        # Get market analysis from trading system
        analysis = trading_system.analyze_market(symbol)
        if not analysis:
            return {
                'type': 'NEUTRAL',
                'confidence': 0,
                'timestamp': datetime.now().isoformat()
            }
        
        return {
            'type': analysis['signal'],
            'confidence': round(analysis['confidence'] * 100, 2),
            'timestamp': analysis['timestamp']
        }
    except Exception as e:
        logger.error(f"Error getting trading signals for {symbol}: {str(e)}")
        return {
            'type': 'NEUTRAL',
            'confidence': 0,
            'timestamp': datetime.now().isoformat()
        }

@app.route('/market')
def market_dashboard():
    symbols = load_symbols()
    return render_template(
        'market_dashboard.html',
        subscribed_symbols=[{'symbol': s} for s in symbols],
        signals={}
    )

# Add WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    if 'user_id' not in session:
        return False
    return websocket_handler.handle_connect(session['user_id'])

@socketio.on('disconnect')
def handle_disconnect():
    if 'user_id' in session:
        websocket_handler.handle_disconnect(session['user_id'])

@socketio.on('subscribe_symbol')
def handle_subscribe(data):
    if 'user_id' not in session:
        return False
    return websocket_handler.subscribe_symbol(session['user_id'], data['symbol'])

@socketio.on('unsubscribe_symbol')
def handle_unsubscribe(data):
    if 'user_id' not in session:
        return False
    return websocket_handler.unsubscribe_symbol(session['user_id'], data['symbol'])

@app.route("/api/trade", methods=["POST"])
def api_trade():
    """API endpoint for executing trades"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    data = request.get_json()
    symbol = data.get('symbol')
    trade_type = data.get('trade_type')
    quantity = data.get('quantity')
    
    if not all([symbol, trade_type, quantity]):
        return jsonify({'success': False, 'message': 'Missing required parameters'}), 400
    
    success, message = execute_trade(session['user_id'], symbol, trade_type, quantity)
    return jsonify({'success': success, 'message': message})

@app.route("/legal")
def legal():
    """Legal information page"""
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    return render_template("legal.html", 
                         user=get_user_by_id(session["user_id"]))

@app.route("/subscription")
def subscription():
    """Subscription plans page"""
    # Define subscription plans
    plans = [
        {
            "name": "Basic",
            "price": "₹999",
            "period": "month",
            "features": [
                "Basic Market Analysis",
                "Daily Trading Signals",
                "Email Notifications",
                "Basic Technical Indicators"
            ],
            "id": "basic"
        },
        {
            "name": "Pro",
            "price": "₹2,499",
            "period": "month",
            "features": [
                "Advanced Market Analysis",
                "Real-time Trading Signals",
                "Priority Email Support",
                "Advanced Technical Indicators",
                "Custom Alerts",
                "Market News Updates"
            ],
            "popular": True,
            "id": "pro"
        },
        {
            "name": "Premium",
            "price": "₹4,999",
            "period": "month",
            "features": [
                "All Pro Features",
                "1-on-1 Trading Support",
                "Custom Strategy Development",
                "Portfolio Analysis",
                "Risk Management Tools",
                "VIP Market Insights"
            ],
            "id": "premium"
        }
    ]
    
    # Get user if authenticated, otherwise pass None
    user = get_user_by_id(session["user_id"]) if "user_id" in session else None
    
    return render_template("subscription.html", 
                         user=user,
                         plans=plans)

@app.route("/subscribe/<plan_id>", methods=["POST"])
def subscribe(plan_id):
    """Handle subscription requests"""
    if "user_id" not in session:
        return jsonify({"error": "Please login to subscribe"}), 401
        
    user = get_user_by_id(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
        
    # Validate plan_id
    valid_plans = ["basic", "pro", "premium"]
    if plan_id not in valid_plans:
        return jsonify({"error": "Invalid subscription plan"}), 400
        
    try:
        # Here you would typically:
        # 1. Process payment
        # 2. Update user's subscription status in database
        # 3. Send confirmation email
        
        # For now, we'll just update the session
        session['subscription'] = {
            'plan': plan_id,
            'started_at': datetime.now().isoformat()
        }
        
        return jsonify({
            "success": True,
            "message": f"Successfully subscribed to {plan_id} plan",
            "redirect": url_for("dashboard")
        })
        
    except Exception as e:
        print(f"Error processing subscription: {str(e)}")
        return jsonify({"error": "Failed to process subscription. Please try again."}), 500

# Load symbols from file
def load_symbols():
    with open('symbols.json') as f:
        return json.load(f)

ALL_SYMBOLS = load_symbols()

# Background price updater
async def background_price_updater(handler):
    while True:
        symbols = load_symbols()  # Reload in case file changes
        for symbol in symbols:
            try:
                data = await handler.get_latest_price_data(symbol)
                handler.price_cache[f'{symbol}_price'] = (datetime.now(), data)
            except Exception as e:
                logger.error(f'Error updating {symbol}: {e}')
        await asyncio.sleep(10)

# Start the background updater
def start_background_updater(handler):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(background_price_updater(handler))
    loop.run_forever()

# Start the background updater thread after websocket_handler is created
threading.Thread(target=start_background_updater, args=(websocket_handler,), daemon=True).start()

@app.route('/favicon.ico')
def favicon():
    return send_file('static/favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/health')
def health_check():
    """Health check endpoint for Render monitoring"""
    try:
        # Check database connection
        db = get_db()
        db.execute('SELECT 1')
        
        # Check Redis connection
        redis_client.ping()
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'services': {
                'database': 'connected',
                'redis': 'connected'
            }
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

if __name__ == "__main__":
    socketio.run(app, debug=True)
