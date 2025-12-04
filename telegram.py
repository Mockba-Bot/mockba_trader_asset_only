import os
import re
import sys
import time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'machine_learning')))
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import telebot
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.db_ops import upsert_setting, get_all_settings, initialize_database_tables, get_setting
from futures_perps.trade.apolo.main import process_signal as run_process_signal  # Rename to avoid conflict
import json
from datetime import timedelta

# Load environment variables
load_dotenv()
initialize_database_tables()

# Bot init
API_TOKEN = os.getenv("API_TOKEN")
bot = telebot.TeleBot(API_TOKEN)
gp1 = ""  # global setting key


def is_float(value):
    try:
        float(value)
        return True
    except ValueError:
        return False

def is_integer(value):
    try:
        int(value)
        return True
    except ValueError:
        return False


def translate(text, chat_id):
    lang = os.getenv("BOT_LANGUAGE", "en").lower()
    try:
        translated = GoogleTranslator(source='auto', target=lang).translate(text)
        return translated
    except Exception as e:
        print(f"Translation error: {e}")
        return text


# === Message Handlers ===

@bot.message_handler(commands=['start'])
def command_start(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    nom = m.chat.first_name
    text = translate("Welcome to Mockba! With this bot, you trade against Apolo Dex.", cid)
    bot.send_message(cid, f"{text}. {nom} - {cid}")
    command_list(m)


@bot.message_handler(commands=['list'])
def command_list(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    buttons = [
        [InlineKeyboardButton(translate("⚙️ Settings", cid), callback_data="Settings")],
        [InlineKeyboardButton(translate("📡  Process Signal", cid), callback_data="ProcessSignal")],
        [InlineKeyboardButton(translate("📋  List All Settings", cid), callback_data="ListSettings")]
    ]
    bot.send_message(cid, translate("Available options.", cid), reply_markup=InlineKeyboardMarkup(buttons))


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.message.chat.type != 'private': return
    cid = call.message.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    options = {
        'List': command_list,
        'Settings': settings,
        'set_asset': set_asset,
        'set_risk': set_risk,
        'set_interval': set_interval,
        'set_min_tp': set_min_tp,
        'set_min_sl': set_min_sl,
        'set_auto_trade': set_auto_trade,
        'set_indicator': set_indicator,
        'set_leverage': set_leverage,
        'set_prompt': set_prompt,
        'ListSettings': ListSettings,
        'ProcessSignal': process_signal
    }
    func = options.get(call.data)
    if func:
        func(call.message)


# === Helper UI Functions ===
def settings(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    labels = {
        "set_asset": "💰 Asset",
        "set_risk": "⚠️ Risk Level",
        "set_interval": "⏱️ Interval",
        "set_min_tp": "📈 Min Take Profit",
        "set_min_sl": "📉 Min Stop Loss",
        "set_auto_trade": "🤖 Auto Trade",
        "set_indicator": "📊 Indicator",
        "set_leverage": "⚖️ Leverage",
        "set_prompt": "💬 Prompt Text"
    }
    buttons = [[InlineKeyboardButton(translate(v, cid), callback_data=k)] for k, v in labels.items()]
    bot.send_message(cid, translate("Available options.", cid), reply_markup=InlineKeyboardMarkup(buttons))


# === Validation & Input Handling ===

def upsert_assets(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    valor = m.text.strip()
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    markup = types.ReplyKeyboardMarkup(row_width=1)
    markup.add(types.KeyboardButton('/list'))

    if valor.upper() == "CANCEL":
        bot.send_message(cid, translate("Operation cancelled.", cid), reply_markup=markup)
        return

    global gp1
    valid, error_msg = True, ""

    if gp1 == "asset":
        if not re.match(r"^PERP_[A-Z0-9]+_USDC$", valor):
            valid, error_msg = False, "Invalid asset format. Use: PERP_BTC_USDC"
    elif gp1 == "risk_level":
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, "Risk must be a positive number (e.g., 1.5)"
    elif gp1 in ("min_tp", "min_sl"):
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, f"Min {'TP' if 'tp' in gp1 else 'SL'} must be positive"
    elif gp1 == "leverage":
        if not is_integer(valor) or not (1 <= int(valor) <= 50):
            valid, error_msg = False, "Leverage must be integer 1–50"
    elif gp1 == "auto_trade":
        if valor not in ("True", "False"):
            valid, error_msg = False, "Auto Trade must be 'True' or 'False'"
    elif gp1 == "interval":
        if not re.match(r"^\d+[mhd]$", valor.lower()):
            valid, error_msg = False, "Invalid interval (e.g., 15m, 1h)"
    # prompt_text: no validation

    if not valid:
        bot.send_message(cid, translate(f"❌ {error_msg}. Try again:", cid), reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(cid, upsert_assets)
        return

    upsert_setting(gp1, valor)
    bot.send_message(cid, translate(f"✅ {gp1} set to {valor}.", cid), reply_markup=markup)  # ← NO Markdown!


# === Setting Entry Points ===

def set_asset(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "asset"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter asset in format: PERP_BTC_USDC", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)

def set_risk(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "risk_level"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter risk level (e.g., 1.5 for 1.5%)", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)

def set_interval(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "interval"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter interval (e.g., 15m, 1h, 4h)", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)

def set_min_tp(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "min_tp"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter min TP % (e.g., 1.0)", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)

def set_min_sl(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "min_sl"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter min SL % (e.g., 1.0)", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)

def set_auto_trade(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "auto_trade"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    markup = types.ReplyKeyboardMarkup(row_width=2)
    markup.add("True", "False", "CANCEL")
    bot.send_message(cid, translate("Select Auto Trade:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_indicator(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "indicator"  # ⚠️ FIXED: was "auto_trade" before!
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    markup = types.ReplyKeyboardMarkup(row_width=1)
    for opt in ['Trend-Following', 'Volatility Breakout', 'Momentum Reversal', 'Momentum + Volatility', 'Hybrid', 'Advanced', 'Router', 'CANCEL']:
        markup.add(opt)
    bot.send_message(cid, translate("Select Indicator:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_leverage(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "leverage"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter leverage (e.g., 5)", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)

def set_prompt(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "prompt_text"
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(m.chat.id): return
    bot.send_message(m.chat.id, translate("Enter prompt text:", m.chat.id))
    bot.register_next_step_handler_by_chat_id(m.chat.id, upsert_assets)


# === Main Actions ===

def process_signal(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    bot.send_message(cid, translate("Processing signal with LLM...", cid))
    try:
        result = run_process_signal()  # renamed import
    except Exception as e:
        result = f"Error: {str(e)}"

    # ⚠️ DO NOT USE MARKDOWN HERE — result may contain *, _, etc.
    bot.send_message(cid, translate(f"Signal processed. Result: {result}", cid))

    auto_trade = get_setting("auto_trade")
    time.sleep(3)
    if auto_trade and auto_trade.lower() == 'false':
        bot.send_message(cid, translate("Auto Trade is disabled. Please execute the trade manually.", cid))
    # if is approved and auto_trade is true, send the message (handled in run_process_signal)
    if auto_trade and auto_trade.lower() == 'true':
        bot.send_message(cid, translate("Auto Trade is enabled. Trade execution handled by the signal processor.", cid))    


def ListSettings(m):
    if m.chat.type != 'private':
        return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        return
    
    bot.send_message(cid, translate("Fetching all settings...", cid))

    settings = get_all_settings()
    if not settings:
        bot.send_message(cid, translate("No settings found.", cid))
        return

    lines = ["🔹 <b>Current Bot Settings</b>"]
    for key, value in sorted(settings.items()):
        # Format key in title case (optional)
        display_key = key.replace("_", " ").title()
        lines.append(f"▸ {display_key}: <code>{value}</code>")

    # Join with newlines
    settings_text = "\n".join(lines)

    # Use HTML mode for safe formatting (bold + code blocks)
    try:
        bot.send_message(cid, settings_text, parse_mode='HTML')
    except Exception:
        # Fallback to plain text if HTML fails
        plain_text = "Current Settings:\n" + "\n".join(f"{k}: {v}" for k, v in sorted(settings.items()))
        bot.send_message(cid, plain_text)


# Start polling
if __name__ == "__main__":
    bot.polling()