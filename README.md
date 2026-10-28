# Alpaca Webhook Server

Receives TradingView webhook alerts and places trades on Alpaca.

## Setup

### 1. Install Python dependencies

```bash
cd alpaca-webhook
pip install -r requirements.txt
```

### 2. Configure your API keys

Copy the example env file and fill in your values:

```bash
copy .env.example .env
```

Then edit `.env` with your actual Alpaca API keys (get them from https://app.alpaca.markets).

### 3. Run the server locally

```bash
python server.py
```

Server runs at `http://localhost:5000`

### 4. Expose to the internet (for TradingView webhooks)

TradingView needs a public HTTPS URL. For testing, use ngrok:

```bash
ngrok http 5000
```

This gives you a URL like `https://abc123.ngrok.io` - use this in TradingView alerts.

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook` | POST | Receives TradingView alerts |
| `/health` | GET | Health check |
| `/account` | GET | Check Alpaca account info |
| `/settings` | GET | Web page for viewing and changing the trading settings (password-protected) |
| `/api/settings` | GET / POST | The same settings as JSON (password-protected) |

## Settings page

Open `/settings` in a browser to view and change the trading settings without
editing code or redeploying:

| Setting | Default | What it does |
|---|---|---|
| Trade size | 1% | Share of buying power spent on each buy |
| Minimum order | $1.00 | Skip a buy if the trade size works out below this |
| Extended-hours limit buffer | 5% | Outside market hours, buy this far above / sell this far below the alert price |
| Take-profit | 0% (off) | After a buy, place a sell limit this far above the fill price |
| Sell-limit offset | 0% | For `sell_limit` alerts, sell this far above the alert price |
| Fill check interval | 5 sec | How often to check whether an extended-hours buy filled |
| Fill check timeout | 3600 sec | Stop checking for an extended-hours fill after this long |

Changes apply to the next alert. Each value is checked against an allowed
range, and a bad value saves nothing.

- **Password:** set `ADMIN_PASSWORD`. The browser asks for it (any username
  works). Without `ADMIN_PASSWORD` the page is turned off.
- **Saving:** values are written to `settings.json`, or to `SETTINGS_PATH` if
  set. On Railway, add a volume mounted at `/data` and set
  `SETTINGS_PATH=/data/settings.json`, otherwise saved values reset on every
  redeploy.
- **Existing env vars:** `TAKE_PROFIT_PERCENT` and `SELL_LIMIT_PERCENT` still
  work as defaults until a value is saved from the page.

## TradingView Alert Message Format

```json
{
    "secret": "your_webhook_secret",
    "symbol": "{{ticker}}",
    "action": "buy",
    "qty": 1
}
```

## Testing

Run the automated tests (no Alpaca account needed; trades are faked):

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Send a test alert with curl:

```bash
curl -X POST http://localhost:5000/webhook -H "Content-Type: application/json" -d "{\"secret\":\"your_webhook_secret\",\"symbol\":\"AAPL\",\"action\":\"buy\",\"qty\":1}"
```
