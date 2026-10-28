# TradingView → Alpaca Trader

A small Python/Flask service that turns TradingView strategy alerts into real
orders on Alpaca, so a trading strategy runs on its own instead of someone
watching charts and placing trades by hand.

**Stack:** Python, Flask, Alpaca API (`alpaca-py`), Gunicorn, Railway, pytest

---

## Why this exists

A friend had a trading strategy he followed on TradingView. Its Pine Script
indicators flagged when to buy, when to take partial profit, and when to get
out, but acting on those signals meant watching charts and placing every
order by hand, often outside market hours. He wanted the strategy to run
itself.

TradingView can send an alert to a URL when a strategy fires, but it can't
place trades with a broker on its own. This project is the missing piece: a
server that receives those alerts and turns each one into the right order on
Alpaca, a broker with a trading API and a free paper-trading account to test
against.

```
TradingView strategy ──alert──▶  this server  ──order──▶  Alpaca
 (Pine Script)          JSON     (Flask, Railway)          (paper account)
                                       │
                                  /settings page
                            (tune trade size, take-profit…)
```

It runs against Alpaca's **paper-trading** account (simulated money) while
the strategy is being proven out.

## What it does

- **Buys** a fixed share of buying power (1% by default) when an alert says
  `buy`.
- **Sells** all of a position (`sell` / `sell_all`), half of it once
  (`sell_half`), or sets a limit sell above the alert price (`sell_limit`).
- **Trades outside market hours** using limit orders, since market orders
  aren't allowed then.
- **Places a take-profit** sell order automatically after a buy, if enabled.
- **Refuses duplicates**: no second buy while one is open or pending, no
  stacked sell orders.
- **Settings page** to change trade size, take-profit and other parameters
  from a phone, without touching code or redeploying.

## Core technical decisions

Most of these came from watching the paper account and fixing what went
wrong; the commit history follows that progression.

### 1. A webhook bridge, not a bot that watches prices

The strategy already lives in TradingView, which has the charts and the Pine
Script logic. Rather than rebuild the indicators in Python and stream market
data, the server only *executes*: TradingView decides when to trade, the
server decides how. That kept the project small and kept the strategy in the
tool my friend already knew how to change.

### 2. A shared secret in the message body

TradingView alerts can't add custom headers, so standard API authentication
isn't possible. Every alert instead carries a secret phrase in its JSON,
which the server checks before doing anything. Requests without it get a
`401`.

### 3. Different order types inside and outside market hours

Many of the strategy's signals fire before the open or after the close.
Alpaca rejects market orders outside regular hours, so the server checks the
time:

- **Market hours:** market orders, which fill immediately.
- **Extended hours:** limit orders flagged for extended-hours trading, priced
  a buffer away from the alert price (buy above it, sell below it) so they
  still fill as the price moves. The buffer started small and was raised to
  5% after orders sat unfilled; it's now adjustable on the settings page.

### 4. Guarding against duplicate orders

TradingView can fire the same signal several times in quick succession, and
an extended-hours limit order can sit unfilled for a while. Without guards,
that meant duplicate buys. Before acting, the server now checks for:

- a buy already in progress for that symbol (an in-process lock closes the
  race between two near-simultaneous alerts),
- an existing position,
- an open, unfilled order on the same side.

Sells don't need the lock: the broker's position is the source of truth, so a
repeated sell either finds nothing left to sell or a sell order already
working.

### 5. Whole-share quantities

Buys are sized in dollars, but not every asset supports fractional shares,
and extended-hours orders need whole shares. Quantities are calculated from
the alert price and rounded down to whole shares, and `sell_half` sells half,
rounded down (or everything if there's only one share). `sell_half` is
remembered per symbol so a repeated alert doesn't keep halving the position.

### 6. Take-profit orders, and the timing around them

After a buy, the server can place a limit sell a set percentage above the
buy price. Two problems came up:

- **During market hours**, placing the sell the instant the buy was submitted
  sometimes got rejected as a potential wash trade. A short delay before
  submitting the take-profit fixed it.
- **Outside market hours**, the buy may not fill for minutes, so there's
  nothing to sell yet. The server starts a background thread that checks the
  order every few seconds and places the take-profit once it fills, giving
  up after an hour.

When a sell signal arrives, any open take-profit order is cancelled first, so
the shares are free to sell and the exit isn't blocked.

### 7. Failing quietly where it's safe

Some symbols TradingView alerts on aren't tradable on Alpaca. The server
remembers them and skips future alerts instead of failing every time. Expected
outcomes ("already have a position", "no position to sell") return `200` with
a `skipped` reason rather than an error, so the logs show what happened and
why.

### 8. A settings page instead of code edits

Tuning the strategy (trade size, take-profit, buffers) originally meant
editing constants and redeploying. The `/settings` page now handles it:

- **Validated:** each value has an allowed range, and one bad value saves
  nothing.
- **Durable:** values are saved to a JSON file on a Railway volume, so they
  survive redeploys.
- **Consistent:** each alert reads one snapshot of the settings, so a change
  can't land halfway through a trade.
- **Protected:** behind a login page tied to an admin password. Updates are
  JSON-only, which blocks cross-site form attacks.

### 9. Tested without touching the broker

The tests swap the Alpaca client for a fake that records orders. That makes it
possible to check, for example, that a 5% trade size on $10,000 at $10 a share
buys exactly 50 shares, without an account or network access.

## Limitations and next steps

- **Market hours are a fixed 9:30–4:00 ET, Monday to Friday**, so holidays
  and early closes aren't detected. Alpaca's market-clock API would fix this.
- **Some state lives in memory** (the duplicate-buy lock, `sell_half`
  tracking, the non-tradable list, take-profit watchers). It resets on
  restart and assumes a single server process. Moving it to a small database
  would make it durable and safe to scale.
- **Paper trading only.** Switching to live money is deliberately a code
  change, not a setting.

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
|---|---|
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Alpaca paper-trading API keys |
| `WEBHOOK_SECRET` | The secret phrase TradingView includes in every alert |
| `ADMIN_PASSWORD` | Password for the settings page (leave unset to turn it off) |
| `SETTINGS_PATH` | Where saved settings go; on Railway, `/data/settings.json` on a volume |

### 3. Run

```bash
python server.py
```

The server runs at `http://localhost:5000`. TradingView needs a public HTTPS
URL, so for local testing expose it with a tunnel such as `ngrok http 5000`.
In production it runs on Railway with Gunicorn (see `Procfile`).

### 4. Point TradingView at it

Set the alert's webhook URL to `https://<your-server>/webhook` and its message
to:

```json
{
  "secret": "your_webhook_secret",
  "symbol": "{{ticker}}",
  "action": "buy",
  "price": {{close}}
}
```

`action` is one of `buy`, `sell`, `sell_all`, `sell_half` or `sell_limit`.
`price` is required for sizing buys and for extended-hours and `sell_limit`
orders.

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/webhook` | POST | Receives TradingView alerts (needs the webhook secret) |
| `/settings` | GET | Settings page (sign-in required) |
| `/login`, `/logout` | GET / POST | Sign in to and out of the settings page |
| `/api/settings` | GET / POST | Settings as JSON (sign-in required) |
| `/account` | GET | Alpaca account balance (sign-in required) |
| `/health` | GET | Health check |

## Settings

| Setting | Default | What it does |
|---|---|---|
| Trade size | 1% | Share of buying power spent on each buy |
| Minimum order | $1.00 | Skip a buy if the trade size works out below this |
| Extended-hours limit buffer | 5% | Outside market hours, buy this far above / sell this far below the alert price |
| Take-profit | 0% (off) | After a buy, place a sell limit this far above the buy price |
| Sell-limit offset | 0% | For `sell_limit` alerts, sell this far above the alert price |
| Fill check interval | 5 sec | How often to check whether an extended-hours buy filled |
| Fill check timeout | 3600 sec | Stop checking for an extended-hours fill after this long |

`TAKE_PROFIT_PERCENT` and `SELL_LIMIT_PERCENT` environment variables act as
defaults until a value is saved from the page, and the page labels them.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest
```

No Alpaca account is needed; orders go to a fake client. To send a test alert
to a running server:

```bash
curl -X POST http://localhost:5000/webhook -H "Content-Type: application/json" \
  -d '{"secret":"your_webhook_secret","symbol":"AAPL","action":"buy","price":190.50}'
```
