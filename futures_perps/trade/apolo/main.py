import json
import requests
import os
import time
import sys
import re
import redis
from pydantic import BaseModel
# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from db.db_ops import  get_setting, get_setting
from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.historical_data import get_historical_data_limit_apolo, get_orderbook, get_funding_rate_history, get_public_liquidations

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Initialize Redis connection
redis_url = os.getenv("REDIS_URL")
if redis_url:
    try:
        redis_client = redis.from_url(redis_url)
        redis_client.ping()
        logger.info("Connected to Redis successfully")
    except redis.ConnectionError as e:
        logger.warning(f"Redis not available (optional caching disabled): {e}")
        redis_client = None
else:
    logger.info("Redis not configured (optional caching disabled)")
    redis_client = None


# Import your executor
from trading_bot.futures_executor_apolo import place_futures_order, get_user_statistics, get_available_balance, ORDERLY_ACCOUNT_ID, ORDERLY_SECRET, ORDERLY_PUBLIC_KEY

from trading_bot.send_bot_message import send_bot_message

# Import your liquidity persistence monitor
from futures_perps.trade.apolo import liquidity_persistence_monitor as lpm


# Helper: Format orderbook as text (not CSV!)
def format_orderbook_as_text(ob: dict) -> str:
    lines = ["Top Bids (price, quantity):"]
    for price, qty in ob.get('bids', [])[:15]:
        lines.append(f"{price},{qty}")
    
    lines.append("\nTop Asks (price, quantity):")
    for price, qty in ob.get('asks', [])[:15]:
        lines.append(f"{price},{qty}")
    
    return "\n".join(lines)


def analyze_with_llm(signal_dict: dict) -> dict:
    """Send to LLM for detailed analysis using fixed prompt structure."""

    # ✅ Get DataFrame with ALL indicators (your function handles timeframe logic)
    df = get_historical_data_limit_apolo(
        symbol=signal_dict['asset'],
        interval=signal_dict['interval'],
        limit=500,
        strategy=signal_dict.get('indicator')
    )
    csv_content = df.to_csv(index=False)  # ← Preserves all columns automatically
    # get the latest close price from the dataframe
    latest_close_price = df['close'].iloc[-1]

    # ✅ Get orderbook as TEXT (not CSV!)
    orderbook = get_orderbook(signal_dict['asset'], limit=20)
    orderbook_content = format_orderbook_as_text(orderbook)  # ← See helper below

    orderly_account_id = ORDERLY_ACCOUNT_ID
    orderly_secret     = ORDERLY_SECRET
    orderly_public_key = ORDERLY_PUBLIC_KEY

    balance = get_available_balance(orderly_secret, orderly_account_id, orderly_public_key) 

    # Get funding history (your actual data shows array of dicts)
    funding_data = get_funding_rate_history(symbol=signal_dict['asset'], limit=50)
    
    # Calculate meaningful funding metrics
    if funding_data and isinstance(funding_data, list):
        funding_rates = [item.get('funding_rate', 0) for item in funding_data]
        current_funding = funding_rates[0] if funding_rates else 0
        avg_funding = sum(funding_rates) / len(funding_rates)
        
        funding_trend = "POSITIVE" if current_funding > avg_funding else "NEGATIVE"
        funding_extreme = abs(current_funding) > 0.0005  # 0.05%
    else:
        current_funding = 0
        funding_trend = "UNKNOWN"
        funding_extreme = False

    # Analyze liquidation clusters (your actual data)
    liquidation_data = get_public_liquidations(symbol=signal_dict['asset'], lookback_hours=24)
    
    if liquidation_data and isinstance(liquidation_data, list):
        total_liquidations = len(liquidation_data)
        
        # Extract liquidation prices and sizes
        liquidation_prices = []
        liquidation_sizes = []
        
        for liquidation in liquidation_data:
            for position in liquidation.get('positions_by_perp', []):
                if position.get('symbol') == signal_dict['asset']:
                    mark_price = position.get('mark_price', 0)
                    position_qty = abs(position.get('position_qty', 0))
                    liquidation_prices.append(mark_price)
                    liquidation_sizes.append(position_qty)
        
        # Find liquidation clusters near current price
        current_price = latest_close_price
        price_range = current_price * 0.02  # 2% range
        nearby_liquidations = sum(1 for price in liquidation_prices 
                                if abs(price - current_price) <= price_range)
        
    else:
        total_liquidations = 0
        nearby_liquidations = 0

    symbol = signal_dict['asset']
    take_profit = signal_dict['min_tp']
    stop_loss = signal_dict['min_sl']
    leverage = signal_dict['leverage']
    risk_level = signal_dict['risk_level']    

    # --- Rest of your prompt logic (unchanged) ---
    analysis_logic = get_setting("prompt_text")

    entry_and_managment = (
        f"\nAnalisys for {symbol}:\n"
        f"Current market price: {latest_close_price}\n"
        f"Suggested Take Profit (TP): {take_profit}% or 3× the Stop Loss distance (1:3 risk-reward ratio)\n"
        f"Suggested Stop Loss (SL): {stop_loss}%, or a dynamic level placed just beyond the most recent swing high/low or key resistance/support zone\n"
        f"Leverage: {leverage}x\n"
        f"Risk Level: {risk_level}% of available balance ({balance} USDC)\n"
        "Based on this, calculate precise entry, TP, and SL levels.\n"
    )

    # Enhanced funding context with actual data
    funding_context = (
        "\nFUNDING RATE ANALYSIS (real data):\n"
        f"• Current rate: {current_funding:.6f} ({current_funding*10000:.2f} bps)\n"
        f"• Trend: {funding_trend}\n"
        f"• Is extreme: {'YES' if funding_extreme else 'NO'}\n"
        "Interpretation:\n"
        "- Funding >0: Longs pay → potential bearish pressure\n"
        "- Funding <0: Shorts pay → potential bullish pressure\n"
        "- |Funding|>0.05%: Strong contrarian signal\n"
    )

    # Enhanced liquidation context
    liquidation_context = (
        "\nLIQUIDATION CLUSTERS (real data):\n"
        f"• Total 24h: {total_liquidations} liquidations\n"
        f"• Near current price: {nearby_liquidations}\n"
        "Implications:\n"
        "- Multiple liquidations nearby: high volatility zone\n"
        "- Smart money may hunt stops at these levels\n"
        "- Consider placing SL outside liquidation clusters\n"
    )

    language = os.getenv("BOT_LANGUAGE", "en")

    response_format = (
        "\nReturn ONLY a valid JSON object with the following keys:\n"
        "- symbol: str (e.g., 'PERP_BTC_USDC')\n"
        "- side: str ('BUY' or 'SELL')\n"
        "- entry: float (use current market price as base)\n"
        "- take_profit: float\n"
        "- stop_loss: float\n"
        "- approved: bool (true if trade is approved, false otherwise)\n"
        "- resume_of_analysis: str (explanation why the trade is rejected or approved)\n"
        f" - respond in the user language defined as {language}\n"
        "\nOnly pure JSON."
    )

    prompt = analysis_logic + entry_and_managment + funding_context + liquidation_context +  response_format

    # Debug the prompt
    logger.debug(f"LLM Prompt:\n{prompt}\n--- End of Prompt ---")

    # --- Send to DeepSeek ---
    response = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('DEEP_SEEK_API_KEY')}"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "user", "content": f"Candles (CSV format):\n{csv_content}"},
                {"role": "user", "content": f"Orderbook:\n{orderbook_content}"},
                {"role": "user", "content": funding_context},
                {"role": "user", "content": liquidation_context}
            ],
            "temperature": 0.0,
            "max_tokens": 500
        }
    )
    
    if response.status_code == 200:
        content = response.json()['choices'][0]['message']['content']
        # Check from response format the resume_of_analysis, simple return from json
        try:
            json_start = content.find('{')
            json_end = content.rfind('}') + 1
            json_str = content[json_start:json_end]
            result = json.loads(json_str)
            return {
                "approved": True,
                "analysis": content,
                **result
            }
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM JSON response: {e}")
            return {
                "approved": False,
                "analysis": content
            }
    else:
        logger.error(f"LLM request failed with status {response.status_code}: {response.text}")
        return {
            "approved": False,
            "analysis": f"LLM request failed with status {response.status_code}"
        }
        

def process_signal():
    """
    Main entry point for signal processing.
    Called by Telegram bot. Must return a string.
    """
    try:
        # --- Fetch required settings ---
        asset = get_setting("asset")
        interval = get_setting("interval")
        min_tp = get_setting("min_tp")
        min_sl = get_setting("min_sl")
        leverage = get_setting("leverage")
        risk_level = get_setting("risk_level")
        indicator = get_setting("indicator")

        # --- Validate settings ---
        missing = []
        if not asset: missing.append("asset")
        if not interval: missing.append("interval")
        if not min_tp: missing.append("min_tp")
        if not min_sl: missing.append("min_sl")
        if not leverage: missing.append("leverage")
        if not risk_level: missing.append("risk_level")

        if missing:
            return f"❌ Missing settings: {', '.join(missing)}. Please configure them via /list."

        # --- Convert types ---
        try:
            min_tp = float(min_tp)
            min_sl = float(min_sl)
            leverage = int(leverage)
            risk_level = float(risk_level)
        except (ValueError, TypeError) as e:
            return f"❌ Invalid setting format: {str(e)}"

        # --- Build signal dict ---
        signal_dict = {
            "asset": asset,
            "interval": interval,
            "min_tp": min_tp,
            "min_sl": min_sl,
            "leverage": leverage,
            "risk_level": risk_level,
            "indicator": indicator or "Trend-Following",
        }

        # --- Call LLM analyzer ---
        llm_result = analyze_with_llm(signal_dict)

        # --- Format response ---
        if isinstance(llm_result, dict) and llm_result.get("approved"):
            try:
                return (
                    f"✅ TRADE APPROVED\n"
                    f"• Symbol: {llm_result['symbol']}\n"
                    f"• Side: {llm_result['side']}\n"
                    f"• Entry: {float(llm_result['entry']):.6f}\n"
                    f"• TP: {float(llm_result['take_profit']):.6f}\n"
                    f"• SL: {float(llm_result['stop_loss']):.6f}\n"
                    f"• Reason: {llm_result.get('resume_of_analysis', 'N/A')}"
                )
            except (KeyError, ValueError, TypeError) as e:
                return f"⚠️ Trade approved but malformed output: {str(e)}"
        else:
            reason = "Unknown"
            if isinstance(llm_result, dict):
                reason = llm_result.get("analysis", "No analysis provided")
            elif isinstance(llm_result, str):
                reason = llm_result
            return f"❌ TRADE REJECTED\n• Reason: {str(reason)[:300]}..."  # Truncate long responses

    except Exception as e:
        logger.exception("Error in process_signal")
        return f"🔥 Internal error: {str(e)}"