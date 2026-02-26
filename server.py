import os
import time
import threading
from datetime import datetime
import pytz
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Initialize Alpaca client (paper=True means paper trading)
trading_client = TradingClient(
    os.getenv("APCA_API_KEY_ID"),
    os.getenv("APCA_API_SECRET_KEY"),
    paper=True  # Change to False for live trading
)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Percentage of account to use per trade
TRADE_PERCENT = 0.01  # 1% of account balance
MIN_NOTIONAL = 1.00   # Minimum dollar amount for an order
LIMIT_PRICE_BUFFER = 0.05  # 5% buffer for limit orders

# Take-profit: automatically place a sell limit after a market-hours buy fills
# Set to 0 to disable, e.g. 0.05 = 5% above fill price
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "0"))

# Track non-tradable assets to avoid repeated failures
non_tradable_assets = set()

# Track symbols with pending orders to prevent race conditions
pending_orders = {}
pending_lock = threading.Lock()

# Track symbols that have already had a sell_half (reset on buy or sell_all)
sell_half_taken = set()

def is_market_open():
    """Check if US stock market is currently open (regular hours)."""
    et = pytz.timezone('US/Eastern')
    now = datetime.now(et)
    # Market hours: 9:30 AM - 4:00 PM ET, Mon-Fri
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

# Polling config for after-hours buy fill detection
POLL_INTERVAL = 5      # seconds between checks
POLL_TIMEOUT = 3600    # max seconds to poll (60 minutes)

def poll_for_buy_fill(order_id, symbol, qty, buy_price):
    """Background thread: poll until an after-hours buy fills, then place take-profit."""
    print(f"POLL START: Watching order {order_id} for {symbol} (every {POLL_INTERVAL}s, timeout {POLL_TIMEOUT}s)")
    elapsed = 0
    while elapsed < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            order = trading_client.get_order_by_id(order_id)
            status = str(order.status).lower()
            
            if status == "filled":
                fill_price = float(order.filled_avg_price) if order.filled_avg_price else buy_price
                tp_price = round(fill_price * (1 + TAKE_PROFIT_PERCENT), 2)
                print(f"POLL FILLED: {symbol} buy filled at ${fill_price}, placing take-profit at ${tp_price}")
                try:
                    tp_order = trading_client.submit_order(
                        LimitOrderRequest(
                            symbol=symbol,
                            qty=int(float(order.filled_qty)),
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC,
                            limit_price=tp_price
                        )
                    )
                    print(f"TAKE-PROFIT: Placed sell limit for {symbol} at ${tp_price}, Order ID: {tp_order.id}")
                except Exception as tp_err:
                    print(f"WARNING: Failed to place take-profit for {symbol}: {tp_err}")
                return
            
            elif status in ["cancelled", "canceled", "expired", "rejected"]:
                print(f"POLL STOP: {symbol} buy order {status}, no take-profit needed")
                return
            
            else:
                print(f"POLL CHECK: {symbol} order status={status} ({elapsed}s elapsed)")
        
        except Exception as poll_err:
            print(f"POLL ERROR: {symbol} - {poll_err}")
    
    print(f"POLL TIMEOUT: {symbol} buy order not filled after {POLL_TIMEOUT}s, stopping watch")

def get_limit_price(base_price, side):
    """Calculate limit price from TradingView price with buffer."""
    if side == OrderSide.BUY:
        # Buy slightly above current price to ensure fill
        limit_price = round(base_price * (1 + LIMIT_PRICE_BUFFER), 2)
    else:
        # Sell slightly below current price to ensure fill
        limit_price = round(base_price * (1 - LIMIT_PRICE_BUFFER), 2)
    
    print(f"TradingView price: ${base_price}, Limit price: ${limit_price}")
    return limit_price

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives webhook from TradingView and places order on Alpaca.
    Buys using 1% of account balance (notional order).
    
    Expected JSON payload:
    {
        "secret": "your_webhook_secret",
        "symbol": "AAPL",
        "action": "buy" or "sell"
    }
    """
    data = request.json
    
    # Log incoming webhook for debugging
    print(f"Received webhook: {data}")
    
    # Verify the secret passphrase
    if data.get("secret") != WEBHOOK_SECRET:
        print("ERROR: Invalid secret")
        return jsonify({"error": "Unauthorized"}), 401
    
    symbol = data.get("symbol")
    action = data.get("action")  # "buy" or "sell"
    tv_price = data.get("price")  # Price from TradingView
    
    if not symbol or not action:
        return jsonify({"error": "Missing symbol or action"}), 400
    
    # Skip known non-tradable assets
    if symbol in non_tradable_assets:
        print(f"SKIP: {symbol} is known non-tradable asset")
        return jsonify({
            "status": "skipped",
            "reason": f"{symbol} is not tradable on Alpaca",
            "symbol": symbol
        }), 200
    
    action_lower = action.lower()
    
    # Determine if this is a buy or sell action
    is_sell = action_lower in ["sell", "sell_half", "sell_all"]
    
    # Only lock BUY orders to prevent duplicates
    # SELL orders don't need locking - they're idempotent (server checks position exists)
    if not is_sell:
        with pending_lock:
            if symbol in pending_orders:
                print(f"SKIP: {symbol} has pending order, avoiding race condition")
                return jsonify({
                    "status": "skipped",
                    "reason": f"Pending order for {symbol}",
                    "symbol": symbol
                }), 200
            pending_orders[symbol] = True
    
    try:
        side = OrderSide.SELL if is_sell else OrderSide.BUY
        
        # Check if market is open for order type decision
        market_open = is_market_open()
        print(f"Market open: {market_open}")
        
        # For SELL orders, check if we have an open position first
        if is_sell:
            try:
                # Check for existing open/pending sell orders
                open_orders = trading_client.get_orders(
                    filter=GetOrdersRequest(
                        status=QueryOrderStatus.OPEN,
                        symbols=[symbol],
                        side=OrderSide.SELL
                    )
                )
                if open_orders:
                    # Stop-loss/sell_all: cancel existing sell orders (e.g. take-profit) and proceed
                    if action_lower in ["sell", "sell_all"]:
                        for o in open_orders:
                            trading_client.cancel_order_by_id(o.id)
                            print(f"CANCELLED existing sell order {o.id} for {symbol} (replacing with stop-loss)")
                    else:
                        pending_qty = sum(float(o.qty) - float(o.filled_qty) for o in open_orders)
                        print(f"SKIP: {symbol} already has {len(open_orders)} open sell order(s) ({pending_qty} shares pending)")
                        return jsonify({
                            "status": "skipped",
                            "reason": f"Existing sell order(s) for {symbol} ({pending_qty} shares pending)",
                            "symbol": symbol
                        }), 200

                position = trading_client.get_open_position(symbol)
                total_qty = float(position.qty)
                
                # Determine quantity to sell based on action
                # Use whole shares for sell_half to support non-fractionable assets
                if action_lower == "sell_half":
                    # Check if we already took partial profit on this position
                    if symbol in sell_half_taken:
                        print(f"SKIP: Already took partial profit on {symbol}")
                        return jsonify({
                            "status": "skipped",
                            "reason": f"Already took partial profit on {symbol}",
                            "symbol": symbol
                        }), 200
                    
                    qty = int(total_qty / 2)
                    if qty < 1:
                        qty = int(total_qty)  # Sell all if too small to split
                    print(f"SELL HALF: {qty} of {total_qty} shares of {symbol}")
                    
                    # Mark that we've taken partial profit
                    sell_half_taken.add(symbol)
                else:
                    qty = int(total_qty)  # sell or sell_all = sell everything (whole shares for non-fractionable)
                    print(f"SELL ALL: {qty} shares of {symbol}")
                    
                    # Reset sell_half tracking (position closed)
                    sell_half_taken.discard(symbol)
                
                if market_open:
                    order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=side,
                        time_in_force=TimeInForce.DAY
                    )
                else:
                    # Extended hours - use limit order with TradingView price
                    limit_price = get_limit_price(tv_price, side)
                    print(f"Extended hours SELL - limit price: ${limit_price}")
                    order_data = LimitOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=side,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price,
                        extended_hours=True
                    )
            except Exception as pos_error:
                # No position found - skip the sell
                print(f"SKIP: No position in {symbol} to sell")
                return jsonify({
                    "status": "skipped",
                    "reason": f"No position in {symbol}",
                    "symbol": symbol
                }), 200
        else:
            # BUY order - check if we already have a position (prevent duplicates)
            try:
                existing_position = trading_client.get_open_position(symbol)
                print(f"SKIP: Already have position in {symbol} ({existing_position.qty} shares)")
                return jsonify({
                    "status": "skipped",
                    "reason": f"Already have position in {symbol}",
                    "symbol": symbol,
                    "existing_qty": str(existing_position.qty)
                }), 200
            except:
                pass  # No position exists, continue checks
            
            # Check for existing open/pending buy orders (e.g. unfilled limit orders)
            open_buy_orders = trading_client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[symbol],
                    side=OrderSide.BUY
                )
            )
            if open_buy_orders:
                pending_qty = sum(float(o.qty) - float(o.filled_qty) for o in open_buy_orders)
                print(f"SKIP: {symbol} already has {len(open_buy_orders)} open buy order(s) ({pending_qty} shares pending)")
                return jsonify({
                    "status": "skipped",
                    "reason": f"Existing buy order(s) for {symbol} ({pending_qty} shares pending)",
                    "symbol": symbol
                }), 200
            
            # Calculate 1% of account balance
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            notional_amount = round(buying_power * TRADE_PERCENT, 2)
            
            # Check minimum notional
            if notional_amount < MIN_NOTIONAL:
                print(f"SKIP: Notional ${notional_amount} below minimum ${MIN_NOTIONAL}")
                return jsonify({
                    "status": "skipped",
                    "reason": f"Insufficient funds (${notional_amount} < ${MIN_NOTIONAL})",
                    "symbol": symbol
                }), 200
            
            print(f"Account buying power: ${buying_power:.2f}, Trading ${notional_amount:.2f} ({TRADE_PERCENT*100}%)")
            
            # Reset sell_half tracking for this symbol (new position)
            sell_half_taken.discard(symbol)
            
            # Calculate qty using TradingView price (works for all assets)
            if tv_price and tv_price > 0:
                qty = int(notional_amount / tv_price)
            else:
                qty = 1  # Fallback
            
            if qty < 1:
                print(f"SKIP: Cannot afford 1 share at ${tv_price}")
                return jsonify({
                    "status": "skipped",
                    "reason": f"Cannot afford 1 share at ${tv_price}",
                    "symbol": symbol
                }), 200
            
            if market_open:
                print(f"BUY {qty} shares of {symbol} at ~${tv_price}")
                order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY
                )
            else:
                # Extended hours - use limit order
                limit_price = get_limit_price(tv_price, side)
                print(f"Extended hours BUY - {qty} shares at limit ${limit_price}")
                order_data = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    extended_hours=True
                )
        
        order = trading_client.submit_order(order_data)
        
        print(f"SUCCESS: Order placed - {action} {symbol}, Order ID: {order.id}")
        
        # Place take-profit sell limit after buy
        take_profit_order_id = None
        if not is_sell and TAKE_PROFIT_PERCENT > 0 and tv_price and tv_price > 0:
            if market_open:
                # Market hours: buy fills instantly, place take-profit immediately
                try:
                    tp_price = round(tv_price * (1 + TAKE_PROFIT_PERCENT), 2)
                    tp_order = trading_client.submit_order(
                        LimitOrderRequest(
                            symbol=symbol,
                            qty=qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC,
                            limit_price=tp_price
                        )
                    )
                    take_profit_order_id = str(tp_order.id)
                    print(f"TAKE-PROFIT: Placed sell limit for {symbol} at ${tp_price} (+{TAKE_PROFIT_PERCENT*100}%), Order ID: {tp_order.id}")
                except Exception as tp_error:
                    print(f"WARNING: Failed to place take-profit for {symbol}: {tp_error}")
            else:
                # Extended hours: buy may not fill yet, start polling thread
                t = threading.Thread(
                    target=poll_for_buy_fill,
                    args=(str(order.id), symbol, qty, tv_price),
                    daemon=True
                )
                t.start()
                print(f"POLL THREAD: Started watching {symbol} buy order {order.id} for fill")
        
        response = {
            "status": "success",
            "order_id": str(order.id),
            "symbol": symbol,
            "side": action
        }
        if take_profit_order_id:
            response["take_profit_order_id"] = take_profit_order_id
        
        return jsonify(response)
    
    except Exception as e:
        error_str = str(e)
        print(f"ERROR: {error_str}")
        
        # Check if asset is not tradable - add to blocklist and return 200
        if "not tradable" in error_str.lower() or "42210000" in error_str:
            non_tradable_assets.add(symbol)
            print(f"Added {symbol} to non-tradable list")
            return jsonify({
                "status": "skipped",
                "reason": f"{symbol} is not tradable on Alpaca",
                "symbol": symbol
            }), 200
        
        # Handle insufficient qty as a skip (not an error)
        if "insufficient qty" in error_str.lower() or "40310000" in error_str:
            print(f"SKIP: Insufficient qty for {symbol} (likely concurrent order)")
            return jsonify({
                "status": "skipped",
                "reason": f"Insufficient qty available for {symbol}",
                "symbol": symbol
            }), 200
        
        return jsonify({"error": error_str}), 500
    finally:
        # Only release lock for buy orders (we only lock buys)
        if not is_sell:
            with pending_lock:
                pending_orders.pop(symbol, None)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint to verify server is running."""
    return jsonify({"status": "ok"})


@app.route("/account", methods=["GET"])
def account():
    """Check your Alpaca account status."""
    try:
        account = trading_client.get_account()
        return jsonify({
            "status": account.status,
            "cash": str(account.cash),
            "buying_power": str(account.buying_power),
            "portfolio_value": str(account.portfolio_value)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("Starting webhook server on http://localhost:5000")
    print("Endpoints:")
    print("  POST /webhook  - Receive TradingView alerts")
    print("  GET  /health   - Health check")
    print("  GET  /account  - Check Alpaca account")
    app.run(host="0.0.0.0", port=5000, debug=True)
