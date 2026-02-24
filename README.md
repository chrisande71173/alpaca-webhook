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

Test with curl:

```bash
curl -X POST http://localhost:5000/webhook -H "Content-Type: application/json" -d "{\"secret\":\"your_webhook_secret\",\"symbol\":\"AAPL\",\"action\":\"buy\",\"qty\":1}"
```
