import os
import sys
# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from futures_perps.trade.apolo.historical_data import get_historical_data_limit_apolo
from trading_bot.futures_executor_apolo import get_close_price,  ORDERLY_ACCOUNT_ID

def testing():
    symbol = "PERP_NEAR_USDC"
    interval = "1h"
    indicator = "Hybrid"
    live_price = get_close_price(ORDERLY_ACCOUNT_ID, symbol)
    print(f"Live Price from {symbol}:", live_price)    
    # Get historical data limit and generates and get the last 3 close prices
    # === 1. Fetch market data (50 candles for trend + indicators) ===
    df = get_historical_data_limit_apolo(
        symbol=symbol,
        interval=interval,
        limit=50,
        strategy=indicator
    )
    latest_close = float(df['close'].iloc[-1])
    print(f"Latest Close Price from Historical Data for {symbol}:", latest_close)
    last_3_closes = df['close'].tail(3).tolist()
    last_3_lows = df['low'].tail(3).tolist()
    last_3_highs = df['high'].tail(3).tolist()
    print(f"Last 3 Close Prices from Historical Data for {symbol}:", last_3_closes)
    is_buy_structure = last_3_lows[0] <= last_3_lows[1] <= last_3_lows[2]
    is_sell_structure = last_3_highs[0] >= last_3_highs[1] >= last_3_highs[2]
    print(f"Is Buy Structure: {is_buy_structure}")
    print(f"Is Sell Structure: {is_sell_structure}")

if __name__ == "__main__":
    testing()    