import json
import requests
import os
import sys
from pydantic import BaseModel
# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from db.db_ops import  get_setting
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
    """LLM analyzes full candle context; Python enforces prices and hard rules."""
    from logs.log_config import apolo_trader_logger as logger

    # === 1. Fetch market data (50 candles for trend + indicators) ===
    df = get_historical_data_limit_apolo(
        symbol=signal_dict['asset'],
        interval=signal_dict['interval'],
        limit=50,
        strategy=signal_dict.get('indicator')
    )
    if df is None or len(df) < 20:
        return {
            "approved": False,
            "analysis": "Insufficient historical data",
            "explanation_for_user": "❌ No se pudieron cargar suficientes datos históricos para analizar la señal."
        }

    latest_close = float(df['close'].iloc[-1])
    csv_content = df.to_csv(index=False)

    # === 2. Auxiliary data ===
    orderbook = get_orderbook(signal_dict['asset'], limit=20)
    orderbook_content = format_orderbook_as_text(orderbook)

    balance = get_available_balance(ORDERLY_SECRET, ORDERLY_ACCOUNT_ID, ORDERLY_PUBLIC_KEY)

    funding_data = get_funding_rate_history(symbol=signal_dict['asset'], limit=50)
    current_funding = float(funding_data[0].get('funding_rate', 0)) if funding_data else 0.0

    liquidation_data = get_public_liquidations(symbol=signal_dict['asset'], lookback_hours=24)
    nearby_liquidations = 0
    if liquidation_data:
        current_price = latest_close
        price_range = current_price * 0.02
        for liq in liquidation_data:
            for pos in liq.get('positions_by_perp', []):
                if pos.get('symbol') == signal_dict['asset']:
                    mark = float(pos.get('mark_price', 0))
                    if abs(mark - current_price) <= price_range:
                        nearby_liquidations += 1

    # === 3. Parse risk settings ===
    try:
        min_sl_pct = float(signal_dict['min_sl']) / 100
        min_tp_pct = float(signal_dict['min_tp']) / 100
        leverage = int(signal_dict['leverage'])
        risk_level = float(signal_dict['risk_level'])
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid risk settings: {e}")
        return {
            "approved": False,
            "analysis": f"Invalid settings: {e}",
            "explanation_for_user": "❌ Error en la configuración del riesgo (SL, TP, apalancamiento o saldo)."
        }

    # === 4. Build prompt ===
    user_prompt = get_setting("prompt_text") or ""
    
    hard_rules_note = """
    ⚠️ Nota para el modelo: Tus valores de entry/tp/sl serán revisados y reemplazados por cálculos reales. 
    Tu rol es evaluar SI la acción del precio, el libro de órdenes y los indicadores justifican una señal.
    """

    context = (
        f"Activo: {signal_dict['asset']}\n"
        f"Precio actual: {latest_close:.6f}\n"
        f"Saldo disponible: {balance:.2f} USDC\n"
        f"Apalancamiento: {leverage}x\n"
        f"Nivel de riesgo: {risk_level}%\n"
        f"Tasa de funding actual: {current_funding:.6f}\n"
        f"Liquidaciones cercanas (±2%): {nearby_liquidations}\n\n"
        f"LIBRO DE ÓRDENES:\n{orderbook_content}\n\n"
        f"HISTORIAL DE VELAS (CSV, {len(df)} filas, más reciente al final):\n{csv_content}"
    )

    response_format = """
    Responde EXCLUSIVAMENTE en JSON válido, SIN texto adicional:
        {
        "side": "BUY" | "SELL" | "NONE",
        "approved": true | false,
        "entry": número (sugerido, será ajustado),
        "take_profit": número (sugerido),
        "stop_loss": número (sugerido),
        "resume_of_analysis": "razón clara basada en tendencia, estructura, RSI, BB, libro, funding"
        }
    """

    prompt = user_prompt + hard_rules_note + context + response_format

    # send the prompt to Telegram bot is the value of show_prompt is True
    if get_setting("show_prompt") == "True":
        send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"📝 Prompt enviado al LLM:\n\n{prompt}")  # limit to first 4000 chars

    # === 5. Call LLM (FIX: remove trailing spaces in URL!) ===
    try:
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",  # ← FIXED
            headers={"Authorization": f"Bearer {os.getenv('DEEP_SEEK_API_KEY')}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 600
            },
            timeout=20
        )
    except Exception as e:
        logger.error(f"LLM request failed: {e}")
        return {
            "approved": False,
            "analysis": f"LLM error: {str(e)}",
            "explanation_for_user": "⚠️ Error de conexión con el motor de análisis. Intente más tarde."
        }

    if response.status_code != 200:
        logger.error(f"LLM API error: {response.status_code} - {response.text}")
        return {
            "approved": False,
            "analysis": f"LLM API error: {response.status_code}",
            "explanation_for_user": "⚠️ El servicio de análisis no está disponible temporalmente."
        }

    # === 6. Parse LLM response ===
    try:
        content = response.json()['choices'][0]['message']['content']
        json_start = content.find('{')
        json_end = content.rfind('}') + 1
        llm_result = json.loads(content[json_start:json_end])
    except Exception as e:
        logger.warning(f"LLM JSON parse failed: {e}")
        return {
            "approved": False,
            "analysis": "LLM returned invalid JSON",
            "explanation_for_user": "⚠️ El análisis automático falló: respuesta inválida del sistema de IA."
        }

    llm_side = llm_result.get("side", "NONE")
    llm_approved = bool(llm_result.get("approved", False))
    llm_reason = llm_result.get("resume_of_analysis", "No analysis")

    # === 7. HARD RULES ENFORCED IN PYTHON ===
    last_3_lows = df['low'].tail(3).astype(float).tolist()
    last_3_highs = df['high'].tail(3).astype(float).tolist()
    is_buy_structure = last_3_lows[0] <= last_3_lows[1] <= last_3_lows[2]
    is_sell_structure = last_3_highs[0] >= last_3_highs[1] >= last_3_highs[2]

    bids = sum(float(qty) for _, qty in orderbook.get('bids', [])[:15])
    asks = sum(float(qty) for _, qty in orderbook.get('asks', [])[:15])
    bid_imbalance = bids / asks if asks > 0 else float('inf')
    ask_imbalance = asks / bids if bids > 0 else float('inf')
    min_imbalance = 1.6

    rsi_ok = True
    if 'RSI' in df.columns:
        latest_rsi = float(df['RSI'].iloc[-1])
        if llm_side == "BUY" and latest_rsi > 70:
            rsi_ok = False
        if llm_side == "SELL" and latest_rsi < 30:
            rsi_ok = False

    # Final decision logic
    final_approved = False
    final_side = "NONE"
    entry = latest_close
    stop_loss = take_profit = entry
    explanation_for_user = ""

    if llm_side == "BUY" and llm_approved and is_buy_structure and bid_imbalance >= min_imbalance and rsi_ok:
        swing_low = min(last_3_lows)
        min_sl_distance = entry * min_sl_pct
        stop_loss = min(swing_low * 0.999, entry - min_sl_distance)
        min_tp_distance = entry * min_tp_pct
        take_profit = entry + max(3 * (entry - stop_loss), min_tp_distance)
        final_approved = True
        final_side = "BUY"
        explanation_for_user = (
            "✅ Señal APROBADA para COMPRA.\n"
            "• Estructura alcista confirmada (mínimos ascendentes).\n"
            f"• Fuerte demanda en libro de órdenes ({bid_imbalance:.1f}x más bids que asks).\n"
            f"• RSI en zona segura ({latest_rsi:.1f}).\n"
            f"• TP/SL calculados con gestión de riesgo 1:3."
        )

    elif llm_side == "SELL" and llm_approved and is_sell_structure and ask_imbalance >= min_imbalance and rsi_ok:
        swing_high = max(last_3_highs)
        min_sl_distance = entry * min_sl_pct
        stop_loss = max(swing_high * 1.001, entry + min_sl_distance)
        min_tp_distance = entry * min_tp_pct
        take_profit = entry - max(3 * (stop_loss - entry), min_tp_distance)
        final_approved = True
        final_side = "SELL"
        explanation_for_user = (
            "✅ Señal APROBADA para VENTA.\n"
            "• Estructura bajista confirmada (máximos descendentes).\n"
            f"• Fuerte oferta en libro de órdenes ({ask_imbalance:.1f}x más asks que bids).\n"
            f"• RSI en zona segura ({latest_rsi:.1f}).\n"
            f"• TP/SL calculados con gestión de riesgo 1:3."
        )

    else:
        # Build rejection explanation
        reasons = []
        if llm_side == "BUY" and not is_buy_structure:
            reasons.append("estructura NO alcista (no hay mínimos ascendentes)")
        if llm_side == "SELL" and not is_sell_structure:
            reasons.append("estructura NO bajista (no hay máximos descendentes)")
        if llm_side == "BUY" and bid_imbalance < min_imbalance:
            reasons.append(f"desequilibrio insuficiente en libro ({bid_imbalance:.1f}x < {min_imbalance}x)")
        if llm_side == "SELL" and ask_imbalance < min_imbalance:
            reasons.append(f"oferta insuficiente en libro ({ask_imbalance:.1f}x < {min_imbalance}x)")
        if not rsi_ok:
            reasons.append("RSI en zona de sobrecompra/sobreventa extrema")
        if not llm_approved:
            reasons.append("análisis técnico no confirma la dirección")

        explanation_for_user = (
            "❌ Señal RECHAZADA.\n" +
            ("• " + "\n• ".join(reasons) if reasons else "• No se cumplieron las condiciones mínimas de seguridad.")
        )

    return {
        "approved": final_approved,
        "symbol": signal_dict['asset'],
        "side": final_side,
        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "resume_of_analysis": llm_reason,
        "analysis": content,
        "explanation_for_user": explanation_for_user  # ← NEW: user-friendly summary
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
        #
        min_tp = float(min_tp)
        min_sl = float(min_sl)

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
            if isinstance(llm_result, dict):
                # Prefer the clean analysis summary
                reason = llm_result.get("resume_of_analysis") or llm_result.get("analysis", "No reason provided.")
            else:
                reason = str(llm_result)

            # Clean up if reason starts with JSON (fallback)
            reason = str(reason).strip()
            if reason.startswith("{"):
                # Try to extract resume_of_analysis from raw JSON string
                try:
                    raw_json_start = reason.find('{')
                    raw_json_end = reason.rfind('}') + 1
                    raw_json_str = reason[raw_json_start:raw_json_end]
                    fallback = json.loads(raw_json_str)
                    reason = fallback.get("resume_of_analysis", "Trade rejected by LLM.")
                except:
                    reason = "Trade rejected due to failing hard rules (see analysis)."

            return f"❌ TRADE REJECTED\n• Reason: {reason[:500]}"  # Allow slightly more for clarity

    except Exception as e:
        logger.exception("Error in process_signal")
        return f"🔥 Internal error: {str(e)}"