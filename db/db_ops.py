# db_ops.py

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from logs.log_config import apolo_trader_logger as logger
DB_PATH = "data/trading.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # enables dict-like access
    try:
        yield conn
    finally:
        conn.close()

def initialize_database_tables():
    with get_db_connection() as conn:
        cur = conn.cursor()

        # create table settings
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Insert the default setting if it doesn't exist
        # key: asset, value: PERP_BTC_USDC
        # key: risk_level, value: 1.5
        # key: interval, value: 1h
        # key: min_tp, value: 1.0
        # key: min_sl, value: 1.0
        # key: auto_trade, value: true
        # key: indicator, value: Hybrid
        # key: leverage, value: 5
        default_settings = [
            ('asset', 'PERP_BTC_USDC'),
            ('risk_level', '1.5'),
            ('interval', '1h'),
            ('min_tp', '1.0'),
            ('min_sl', '1.0'),
            ('auto_trade', 'true'),
            ('indicator', 'Hybrid'),
            ('leverage', '5'), 
            ('prompt_text', 'standard')
        ]
        for key, value in default_settings:
            cur.execute("""
                INSERT OR IGNORE INTO settings (key, value)
                VALUES (?, ?);
            """, (key, value))
        
        conn.commit()
        logger.info("✅ SQLite tables initialized.")

# get bot status
def get_bot_status():
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT is_running FROM bot_control WHERE id = 1")
        row = cur.fetchone()
        return bool(row['is_running']) if row else True  # default to True


# start or stop the bot
def startStopBotOp(status: bool):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE bot_control
            SET is_running = ?
            WHERE id = 1
        """, (int(status),))
        conn.commit()

# Def to insert or update settings
def upsert_setting(key: str, value: str):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP;
        """, (key, value))
        conn.commit()

# Def to get setting by key
def get_setting(key: str) -> str | None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row['value'] if row else None 

# Def to get all settings
def get_all_settings() -> dict:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
        return {row['key']: row['value'] for row in rows}                  