import json
import requests
import os
import sys
from pydantic import BaseModel
# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from db.db_ops import  get_setting, get_setting
from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.historical_data import get_historical_data_limit_apolo, get_orderbook, get_funding_rate_history, get_public_liquidations

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

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
        limit=250,
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

    entry_and_management = (
        f"\nAnálisis para {symbol}:\n"
        f"Precio de mercado actual: {latest_close_price}\n"
        f"Take Profit sugerido (TP): {take_profit}% o 3× la distancia del Stop Loss (ratio riesgo-recompensa 1:3)\n"
        f"Stop Loss sugerido (SL): {stop_loss}%, o un nivel dinámico colocado justo más allá del swing alto/bajo más reciente o zona clave de resistencia/soporte\n"
        f"Apalancamiento: {leverage}x\n"
        f"Nivel de Riesgo: {risk_level}% del saldo disponible ({balance} USDC)\n"
        "Basado en esto, calcula niveles precisos de entrada, TP y SL.\n"
    )

    # Contexto de funding mejorado con datos reales
    funding_context = (
        "\nANÁLISIS DE TASA DE FUNDING (datos reales):\n"
        f"• Tasa actual: {current_funding:.6f} ({current_funding*10000:.2f} bps)\n"
        f"• Tendencia: {funding_trend}\n"
        f"• Es extrema: {'SÍ' if funding_extreme else 'NO'}\n"
        "Interpretación:\n"
        "- Funding >0: Longs pagan → presión potencial bajista\n"
        "- Funding <0: Shorts pagan → presión potencial alcista\n"
        "- |Funding|>0.05%: Señal contraria fuerte\n"
    )

    # Contexto de liquidaciones mejorado
    liquidation_context = (
        "\nCLUSTERS DE LIQUIDACIONES (datos reales):\n"
        f"• Total 24h: {total_liquidations} liquidaciones\n"
        f"• Cercanas al precio actual: {nearby_liquidations}\n"
        "Implicaciones:\n"
        "- Múltiples liquidaciones cercanas: zona de alta volatilidad\n"
        "- El dinero inteligente puede cazar stops en estos niveles\n"
        "- Considera colocar SL fuera de clusters de liquidaciones\n"
    )

    language = os.getenv("BOT_LANGUAGE", "en")

    response_format = (
        "\nDevuelve SOLO un objeto JSON válido con las siguientes claves:\n"
        "- symbol: str (ej., 'PERP_BTC_USDC')\n"
        "- side: str ('BUY' o 'SELL')\n"
        "- entry: float (usa el precio de mercado actual como base)\n"
        "- take_profit: float\n"
        "- stop_loss: float\n"
        "- approved: bool (true si el trade está aprobado, false en caso contrario)\n"
        "- resume_of_analysis: str (explicación por qué el trade es rechazado o aprobado)\n"
        f" - responde en el idioma del usuario definido como {language}\n"
        "\nSolo JSON puro."
    )

    prompt = analysis_logic + entry_and_management + funding_context + liquidation_context +  response_format

    # Debug the prompt
    # logger.debug(f"LLM Prompt:\n{prompt}\n--- End of Prompt ---")

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

            # Extract approved flag FROM LLM response
            llm_approved = result.get('approved', False)
            if isinstance(llm_approved, str):
                llm_approved = llm_approved.lower() == 'true'

            return {
                "approved": bool(llm_approved),  # ← Now respects LLM decision
                "analysis": content,
                "symbol": result.get('symbol', signal_dict['asset']),
                "side": result.get('side', 'BUY'),
                "entry": result.get('entry', latest_close_price),
                "take_profit": result.get('take_profit', latest_close_price * 1.01),
                "stop_loss": result.get('stop_loss', latest_close_price * 0.99),
                "resume_of_analysis": result.get('resume_of_analysis', 'Analysis not provided'),
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
                # the signal was approved, if the auto_trade setting is true, place the order
                # and create the dict required to place the order, the values are
                # symbol, side, take_profit, stop_loss, leverage
                if get_setting("auto_trade") == "True":
                    signal_dict = {
                        "symbol": llm_result['symbol'],
                        "side": llm_result['side'],
                        "entry": float(llm_result['entry']),   
                        "take_profit": float(llm_result['take_profit']),
                        "stop_loss": float(llm_result['stop_loss']),
                        "leverage": leverage
                    }
                    place_futures_order(signal_dict)  
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