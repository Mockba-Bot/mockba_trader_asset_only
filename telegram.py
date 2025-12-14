import os
import re
import sys
import time
import threading
import re
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'machine_learning')))
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.db_ops import (
    upsert_setting, get_all_settings, initialize_database_tables, get_setting, 
    add_asset, remove_asset, get_asset_list,
    add_automated_asset, remove_automated_asset, get_automated_asset_list
)
from futures_perps.trade.apolo.main import process_signal as run_process_signal , autotrade # Rename to avoid conflict
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

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(translate("⚙️ Settings", cid), callback_data="Settings"))
    markup.row(InlineKeyboardButton(translate("📡 Process Signal", cid), callback_data="ProcessSignal"))
    markup.row(InlineKeyboardButton(translate("📋 List All Settings", cid), callback_data="ListSettings"))
    
    bot.send_message(cid, translate("Available options.", cid), reply_markup=markup)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.message.chat.type != 'private': return
    cid = call.message.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    # Good practice: Answer callback to stop loading animation
    try:
        bot.answer_callback_query(call.id)
    except:
        pass

    # Determine if we should remove buttons immediately (long tasks) or later (UI transitions)
    immediate_remove = False
    if call.data.startswith("exec_sig:"):
        immediate_remove = True
    
    if immediate_remove:
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except:
            pass

    # Execute logic
    if call.data.startswith("rm_asset:"):
        asset = call.data.split(":", 1)[1]
        confirm_remove_asset(call.message, asset)
    elif call.data.startswith("exec_sig:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset)
    elif call.data.startswith("set_val:"):
        _, key, val = call.data.split(":", 2)
        upsert_setting(key, val)
        
        # Determine next step for navigation
        next_step = None
        next_label = None
        
        if key == "interval": 
            next_step = "set_min_tp"
            next_label = "Next: Min TP ➡️"
        elif key == "auto_trade": 
            next_step = "set_indicator"
            next_label = "Next: Indicator ➡️"
        elif key == "indicator": 
            next_step = "set_leverage"
            next_label = "Next: Leverage ➡️"
        elif key == "show_prompt": 
            next_step = "set_prompt_mode"
            next_label = "Next: Prompt Mode ➡️"
        elif key == "prompt_mode": 
            next_step = "set_order_book_threshold"
            next_label = "Next: Order Book Threshold ➡️"
        
        markup = None
        if next_step:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(translate(next_label, cid), callback_data=next_step))
            
        bot.send_message(cid, translate(f"✅ {key} set to {val}.", cid), reply_markup=markup)
    elif call.data == "auto_trade_auto":
        upsert_setting("auto_trade", "Automatic")
        bot.send_message(cid, translate("✅ Auto Trade set to Automatic.", cid))
        manage_automated_assets(call.message)
    elif call.data.startswith("toggle_auto_asset:"):
        asset = call.data.split(":", 1)[1]
        current_auto = get_automated_asset_list()
        if asset in current_auto:
            remove_automated_asset(asset)
            try: bot.answer_callback_query(call.id, f"Removed {asset}")
            except: pass
        else:
            add_automated_asset(asset)
            try: bot.answer_callback_query(call.id, f"Added {asset}")
            except: pass
        manage_automated_assets(call.message, edit_msg_id=call.message.message_id)
    elif call.data.startswith("add_auto_asset:"):
        asset = call.data.split(":", 1)[1]
        confirm_add_automated_asset(call.message, asset)
    elif call.data.startswith("rm_auto_asset:"):
        asset = call.data.split(":", 1)[1]
        confirm_remove_automated_asset(call.message, asset)
    else:
        options = {
            'List': command_list,
            'Settings': settings,
            'set_asset': set_asset,
            'asset_add': ask_add_asset,
            'asset_remove': ask_remove_asset,
            'manage_automated_assets': manage_automated_assets,
            'auto_asset_add': ask_add_automated_asset,
            'auto_asset_remove': ask_remove_automated_asset,
            'set_risk': set_risk,
            'set_interval': set_interval,
            'set_min_tp': set_min_tp,
            'set_min_sl': set_min_sl,
            'set_auto_trade': set_auto_trade,
            'set_indicator': set_indicator,
            'set_leverage': set_leverage,
            'set_prompt': set_prompt,
            'ListSettings': ListSettings,
            'ProcessSignal': execute_signal,
            'set_show_prompt': set_show_prompt,
            'set_prompt_mode': set_prompt_mode,
            'set_order_book_threshold': set_order_book_threshold
        }
        func = options.get(call.data)
        if func:
            func(call.message)

    # Delayed remove for UI transitions (gives time for new menu/message to appear)
    if not immediate_remove:
        time.sleep(0.5) 
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except:
            pass


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
        "set_prompt_mode": "📝 Prompt Mode",
        "set_order_book_threshold": "📚 Order Book Threshold"
    }
    
    markup = InlineKeyboardMarkup()
    items = list(labels.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(translate(label, cid), callback_data=key) for key, label in items[i:i+2]]
        markup.add(*row)
        
    bot.send_message(cid, translate("Available options.", cid), reply_markup=markup)


# === Validation & Input Handling ===

def upsert_assets(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    valor = m.text.strip()
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if valor.upper() == "CANCEL":
        bot.send_message(cid, translate("Operation cancelled.", cid))
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
    # order book threshold validation
    elif gp1 == "order_book_threshold":
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, "Order Book Threshold must be a positive number (e.g., 1.6)"                

    if not valid:
        bot.send_message(cid, translate(f"❌ {error_msg}. Try again:", cid))
        bot.register_next_step_handler_by_chat_id(cid, upsert_assets)
        return

    upsert_setting(gp1, valor)
    
    # Determine next step for navigation
    next_step = None
    next_label = None
    
    if gp1 == "risk_level": 
        next_step = "set_interval"
        next_label = "Next: Interval ➡️"
    elif gp1 == "min_tp": 
        next_step = "set_min_sl"
        next_label = "Next: Min SL ➡️"
    elif gp1 == "min_sl": 
        next_step = "set_auto_trade"
        next_label = "Next: Auto Trade ➡️"
    elif gp1 == "leverage": 
        next_step = "set_prompt"
        next_label = "Next: Prompt Text ➡️"
    elif gp1 == "prompt_text": 
        next_step = "set_show_prompt"
        next_label = "Next: Show Prompt ➡️"
    elif gp1 == "order_book_threshold": 
        next_step = "Settings"
        next_label = "Finish ✅"

    markup = None
    if next_step:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(translate(next_label, cid), callback_data=next_step))

    bot.send_message(cid, translate(f"✅ {gp1} set to {valor}.", cid), reply_markup=markup)


# === Setting Entry Points ===

def set_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("➕ Add Asset", cid), callback_data="asset_add"))
    markup.add(InlineKeyboardButton(translate("➖ Remove Asset", cid), callback_data="asset_remove"))
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Risk Level ➡️", cid), callback_data="set_risk"))
    
    current_assets = get_asset_list()
    msg = translate("Manage Assets:", cid) + "\n" + ", ".join(current_assets)
    bot.send_message(cid, msg, reply_markup=markup)

def ask_add_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    bot.send_message(cid, translate("Enter asset to ADD (format: PERP_BTC_USDC):", cid))
    bot.register_next_step_handler_by_chat_id(cid, confirm_add_asset)

def confirm_add_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    valor = m.text.strip()
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if valor.upper() == "CANCEL":
        bot.send_message(cid, translate("Operation cancelled.", cid))
        return

    if not re.match(r"^PERP_[A-Z0-9]+_USDC$", valor):
        bot.send_message(cid, translate("❌ Invalid format. Use: PERP_BTC_USDC. Try again:", cid))
        bot.register_next_step_handler_by_chat_id(cid, confirm_add_asset)
        return

    add_asset(valor)
    bot.send_message(cid, translate(f"✅ Asset {valor} added.", cid))
    set_asset(m) # Show menu again

def ask_remove_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    assets = get_asset_list()
    if not assets:
        bot.send_message(cid, translate("No assets to remove.", cid))
        return

    markup = InlineKeyboardMarkup()
    for asset in assets:
        markup.add(InlineKeyboardButton(f"❌ {asset}", callback_data=f"rm_asset:{asset}"))
    
    bot.send_message(cid, translate("Select asset to REMOVE:", cid), reply_markup=markup)

def confirm_remove_asset(m, asset):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    remove_asset(asset)
    bot.send_message(cid, translate(f"✅ Asset {asset} removed.", cid))
    set_asset(m) # Show menu again


def set_risk(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "risk_level"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Interval ➡️", cid), callback_data="set_interval"))
               
    bot.send_message(cid, translate("Enter risk level (e.g., 1.5 for 1.5%)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_interval(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    options = ['5m', '15m', '30m', '1h', '4h', '1d']
    buttons = [InlineKeyboardButton(opt, callback_data=f"set_val:interval:{opt}") for opt in options]
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Min TP ➡️", cid), callback_data="set_min_tp"))
        
    bot.send_message(cid, translate("Select Interval:", cid), reply_markup=markup)

def set_min_tp(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "min_tp"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Min SL ➡️", cid), callback_data="set_min_sl"))
               
    bot.send_message(cid, translate("Enter min TP % (e.g., 1.0)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_min_sl(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "min_sl"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Auto Trade ➡️", cid), callback_data="set_auto_trade"))
               
    bot.send_message(cid, translate("Enter min SL % (e.g., 1.0)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_auto_trade(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("True", callback_data="set_val:auto_trade:True"),
               InlineKeyboardButton("False", callback_data="set_val:auto_trade:False"))
    markup.add(InlineKeyboardButton("Automatic", callback_data="auto_trade_auto"))
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Indicator ➡️", cid), callback_data="set_indicator"))
    
    bot.send_message(cid, translate("Select Auto Trade:", cid), reply_markup=markup)

def manage_automated_assets(m, edit_msg_id=None):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    all_assets = get_asset_list()
    auto_assets = get_automated_asset_list()
    
    markup = InlineKeyboardMarkup()
    
    # Create toggle buttons for each asset
    row = []
    for asset in all_assets:
        is_auto = asset in auto_assets
        status = "✅" if is_auto else "❌"
        btn_text = f"{status} {asset}"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"toggle_auto_asset:{asset}"))
        
        if len(row) == 2: 
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)
            
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Indicator ➡️", cid), callback_data="set_indicator"))
    
    msg_text = translate("Manage Automated Assets (Click to toggle):", cid)
    
    if edit_msg_id:
        try:
            bot.edit_message_text(chat_id=cid, message_id=edit_msg_id, text=msg_text, reply_markup=markup)
        except:
            bot.send_message(cid, msg_text, reply_markup=markup)
    else:
        bot.send_message(cid, msg_text, reply_markup=markup)

def ask_add_automated_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    # Show available assets that are NOT in automated list
    all_assets = get_asset_list()
    current_auto = get_automated_asset_list()
    available = [a for a in all_assets if a not in current_auto]
    
    if not available:
        bot.send_message(cid, translate("No more assets available to add.", cid))
        manage_automated_assets(m)
        return

    markup = InlineKeyboardMarkup()
    for asset in available:
        markup.add(InlineKeyboardButton(f"➕ {asset}", callback_data=f"add_auto_asset:{asset}"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="manage_automated_assets"))
    bot.send_message(cid, translate("Select asset to ADD to Automation:", cid), reply_markup=markup)

def confirm_add_automated_asset(m, asset):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    add_automated_asset(asset)
    bot.send_message(cid, translate(f"✅ Asset {asset} added to automation.", cid))
    manage_automated_assets(m)

def ask_remove_automated_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    assets = get_automated_asset_list()
    if not assets:
        bot.send_message(cid, translate("No automated assets to remove.", cid))
        manage_automated_assets(m)
        return

    markup = InlineKeyboardMarkup()
    for asset in assets:
        markup.add(InlineKeyboardButton(f"❌ {asset}", callback_data=f"rm_auto_asset:{asset}"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="manage_automated_assets"))
    bot.send_message(cid, translate("Select asset to REMOVE from Automation:", cid), reply_markup=markup)

def confirm_remove_automated_asset(m, asset):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    remove_automated_asset(asset)
    bot.send_message(cid, translate(f"✅ Asset {asset} removed from automation.", cid))
    manage_automated_assets(m)


def set_indicator(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    options = ['Trend-Following', 'Volatility Breakout', 'Momentum Reversal', 'Momentum + Volatility', 'Hybrid', 'Advanced', 'Router']
    for opt in options:
        markup.add(InlineKeyboardButton(translate(opt, cid), callback_data=f"set_val:indicator:{opt}"))
    
    # Add reference URL button
    markup.add(InlineKeyboardButton(translate("📚 Reference Indicators", cid), url="https://learning-dex.apolopay.app/docs/strategy-indicators-reference"))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Leverage ➡️", cid), callback_data="set_leverage"))
        
    bot.send_message(cid, translate("Select Indicator:", cid), reply_markup=markup)

def set_leverage(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "leverage"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Prompt Text ➡️", cid), callback_data="set_prompt"))
               
    bot.send_message(cid, translate("Enter leverage (e.g., 5)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_prompt(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "prompt_text"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Show Prompt ➡️", cid), callback_data="set_show_prompt"))
               
    bot.send_message(cid, translate("Enter prompt text:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_show_prompt(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("True", callback_data="set_val:show_prompt:True"),
               InlineKeyboardButton("False", callback_data="set_val:show_prompt:False"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Prompt Mode ➡️", cid), callback_data="set_prompt_mode"))
               
    bot.send_message(cid, translate("Select Show Prompt:", cid), reply_markup=markup)

def set_prompt_mode(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("mixed", callback_data="set_val:prompt_mode:mixed"),
               InlineKeyboardButton("user_only", callback_data="set_val:prompt_mode:user_only"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Order Book Threshold ➡️", cid), callback_data="set_order_book_threshold"))
               
    bot.send_message(cid, translate("Select Prompt Mode:", cid), reply_markup=markup)

def set_order_book_threshold(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "order_book_threshold"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))
               
    bot.send_message(cid, translate("Enter Order Book Threshold (e.g., 1.6)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)


# === Main Actions ===

def execute_signal(m, asset=None):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if asset is None:
        assets = get_asset_list()
        if not assets:
            bot.send_message(cid, translate("❌ No assets configured. Please add assets first.", cid))
            return

        markup = InlineKeyboardMarkup()
        for asset_item in assets:
            markup.add(InlineKeyboardButton(f"📡 {asset_item}", callback_data=f"exec_sig:{asset_item}"))
        
        bot.send_message(cid, translate("Select asset to process:", cid), reply_markup=markup)
        return

    interval = get_setting("interval")

    bot.send_message(cid, translate(f"Processing signal for {asset} interval {interval} with LLM...", cid))
    time.sleep(1)
    try:
        result = run_process_signal(asset_override=asset)  # Pass the selected asset
    except Exception as e:
        result = f"Error: {str(e)}"

    # SIMPLE FIX: Just send as plain text without any parse mode
    try:
        result_str = str(result)
        # Truncate if too long
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "..."
        
        bot.send_message(cid, translate(f"Signal processed for {asset}. Result:\n\n{result_str}", cid))
    except Exception as e:
        bot.send_message(cid, translate(f"Signal processed but error displaying result: {str(e)}", cid))

    auto_trade = get_setting("auto_trade")
    time.sleep(2)
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
        # order book threshold
        ,("📚", "order_book_threshold", "Order Book Thresh")
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
        ("👁️", "show_prompt", "Show Prompt")
    ]
    
    for emoji, key, label in config_keys:
        if key in settings:
            value = settings[key]
            if key == "auto_trade": # add for automatic option
                value = "🤖 Automatic" if value == "Automatic" else ("✅ YES" if str(value).lower() == "true" else "❌ NO")
                if settings[key] == "Automatic":
                     auto_assets = get_automated_asset_list()
                     if auto_assets:
                         value += f"\n   └ 📋 {', '.join(auto_assets)}"
                     else:
                         value += "\n   └ ⚠️ No assets selected"
            elif key == "show_prompt":
                value = "✅ YES" if str(value).lower() == "true" else "❌ NO"
            elif key == "prompt_text" and len(value) > 25:
                value = value[:25] + "..."
            message += f"{emoji} <b>{label}:</b> <code>{value}</code>\n"
    
    # Add timestamp
    from datetime import datetime
    timestamp = datetime.now().strftime("%H:%M:%S")
    message += f"\n⏰ <i>Updated: {timestamp} | Total: {len(settings)} settings</i>"
    
    bot.send_message(cid, message, parse_mode='HTML')



# Start polling
if __name__ == "__main__":
    # Start autotrade in a separate thread to avoid blocking the bot
    t = threading.Thread(target=autotrade, daemon=True)
    t.start()
    bot.polling()