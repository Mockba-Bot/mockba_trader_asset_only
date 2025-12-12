import os
import re
import sys
import time
import html
import re
import json
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
        'ProcessSignal': process_signal,
        'set_show_prompt': set_show_prompt,
        'prompt_mode': set_prompt_mode,
        'set_llm_model': set_llm_model
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
        "set_prompt": "💬 Prompt Text",
        "set_show_prompt": "👁️ Show Prompt",
        "prompt_mode": "📝 Prompt Mode",
       #  "set_llm_model": "🧠 LLM Model"
    }
    buttons = []
    items = list(labels.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(translate(label, cid), callback_data=key) for key, label in items[i:i+2]]
        buttons.append(row)
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
        if not re.match(r"^\d+[mhd]$", valor.lower()) and not valor.lower() in ("5m", "15m", "30m", "1h", "4h", "1d"):
            valid, error_msg = False, "Invalid interval (e.g., 15m, 1h)"
    # show prompt validation
    elif gp1 == "show_prompt":
        if valor not in ("True", "False"):
            valid, error_msg = False, "Show Prompt must be 'True' or 'False'"        
    # prompt mode validation
    elif gp1 == "prompt_mode":
        if valor not in ("mixed", "user_only"):
            valid, error_msg = False, "Prompt Mode must be 'mixed' or 'user_only'"
    # llm model validation
    elif gp1 == "llm_model":
        if valor not in ("deepseek-reasoner", "deepseek-chat"):
            valid, error_msg = False, "LLM Model must be 'deepseek-reasoner' or 'deepseek-chat'"        

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
    markup = types.ReplyKeyboardMarkup(row_width=1)
    for opt in ['5m', '15m', '30m', '1h', '4h', '1d']:
        markup.add(opt)
    bot.send_message(m.chat.id, translate("Enter interval (e.g., 15m, 1h, 4h)", m.chat.id), reply_markup=markup)
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

def set_show_prompt(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "show_prompt"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    markup = types.ReplyKeyboardMarkup(row_width=2)
    markup.add("True", "False", "CANCEL")
    bot.send_message(cid, translate("Select Show Prompt:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets) 

def set_prompt_mode(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "prompt_mode"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    markup = types.ReplyKeyboardMarkup(row_width=1)
    markup.add("mixed", "user_only", "CANCEL")
    bot.send_message(cid, translate("Select Prompt Mode:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_llm_model(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "llm_model"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    markup = types.ReplyKeyboardMarkup(row_width=1)
    for opt in ['deepseek-reasoner', 'deepseek-chat', 'CANCEL']:
        markup.add(opt)
    bot.send_message(cid, translate("Select LLM Model:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)    


# === Main Actions ===

def process_signal(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    asset = get_setting("asset")
    interval = get_setting("interval")

    bot.send_message(cid, translate(f"Processing signal for {asset} interval {interval} with LLM...", cid))
    time.sleep(2)
    try:
        result = run_process_signal()  # renamed import
    except Exception as e:
        result = f"Error: {str(e)}"

    # SIMPLE FIX: Just send as plain text without any parse mode
    try:
        result_str = str(result)
        # Truncate if too long
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "..."
        
        bot.send_message(cid, translate(f"Signal processed. Result:\n\n{result_str}", cid))
    except Exception as e:
        bot.send_message(cid, translate(f"Signal processed but error displaying result: {str(e)}", cid))

    auto_trade = get_setting("auto_trade")
    time.sleep(3)
    if auto_trade and auto_trade.lower() == 'false':
        bot.send_message(cid, translate("Auto Trade is disabled. Please execute the trade manually.", cid))
    if auto_trade and auto_trade.lower() == 'true':
        bot.send_message(cid, translate("Auto Trade is enabled. Trade execution handled by the signal processor.", cid))


def ListSettings(m):
    if m.chat.type != 'private':
        return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        return
    
    settings = get_all_settings()
    
    # Add defaults for missing important settings
    if 'prompt_mode' not in settings:
        settings['prompt_mode'] = os.getenv('PROMPT_MODE', 'mixed')
    
    if not settings:
        bot.send_message(cid, "❌ No settings configured", parse_mode='HTML')
        return
    
    # Build compact message
    message = "<b>⚙️ BOT SETTINGS</b>\n"
    message += "═══════════════════\n\n"
    
    # Trading settings section
    message += "<b>📈 Trading:</b>\n"
    trading_keys = [
        ("💰", "asset", "Asset"),
        ("⏱️", "interval", "Interval"),
        ("📊", "indicator", "Strategy"),
        ("⚖️", "leverage", "Leverage"),
        ("⚠️", "risk_level", "Risk"),
        ("🎯", "min_tp", "Min TP"),
        ("🎯", "min_sl", "Min SL")
    ]
    
    for emoji, key, label in trading_keys:
        if key in settings:
            value = settings[key]
            if key in ["min_tp", "min_sl"]:
                value = f"{value}%"
            elif key == "leverage":
                value = f"{value}x"
            message += f"{emoji} <b>{label}:</b> <code>{value}</code>\n"
    
    message += "\n<b>⚙️ Configuration:</b>\n"
    config_keys = [
        ("🤖", "auto_trade", "Auto Trade"),
        ("💬", "prompt_text", "Prompt"),
        ("🔄", "prompt_mode", "Prompt Mode"),
        ("👁️", "show_prompt", "Show Prompt"),
        ("🧠", "llm_model", "LLM Model")
    ]
    
    for emoji, key, label in config_keys:
        if key in settings:
            value = settings[key]
            if key == "auto_trade":
                value = "✅ ON" if str(value).lower() == "true" else "❌ OFF"
            elif key == "show_prompt":
                value = "✅ YES" if str(value).lower() == "true" else "❌ NO"
            elif key == "prompt_text" and len(value) > 25:
                value = value[:25] + "..."
            elif key == "llm_model":
                value = "🤖" + str(value)
            message += f"{emoji} <b>{label}:</b> <code>{value}</code>\n"
    
    # Add timestamp
    from datetime import datetime
    timestamp = datetime.now().strftime("%H:%M:%S")
    message += f"\n⏰ <i>Updated: {timestamp} | Total: {len(settings)} settings</i>"
    
    bot.send_message(cid, message, parse_mode='HTML')


# Start polling
if __name__ == "__main__":
    bot.polling()