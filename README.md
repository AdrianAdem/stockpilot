# StockPilot

Fully autonomous stock trading bot for Alpaca Paper Trading. Combines technical analysis, whale tracking (13F filings), and Claude AI analysis to make trading decisions.

**PAPER TRADING ONLY** — hardcoded safety check prevents live trading.

## Features

- **3 Strategies**: Momentum, Mean Reversion, Whale Following
- **Claude AI Analysis**: 2-tier (Haiku screening + Sonnet deep analysis)
- **13F Whale Tracking**: Monitors Buffett, Dalio, Soros, Simons, Griffin, Ackman, Loeb, Tepper
- **Risk Management**: Position sizing (Kelly + Fixed Fractional), sector limits, drawdown protection
- **Trailing Stops**: Auto-adjusting stop-losses based on profit levels
- **Telegram Notifications**: Trade alerts, daily summaries, error notifications
- **FastAPI Dashboard**: Portfolio overview, positions, signals, whale tracker, API costs
- **Backtesting**: Historical strategy testing with HTML reports

## Setup

```bash
cp .env.example .env
# Fill in API keys

pip install -r requirements.txt
python -m main
```

## Docker

```bash
docker compose up -d
```

Dashboard: http://localhost:8000

## Architecture

```
main.py          → Orchestrator (15min loop during market hours)
├── data/        → Alpaca, SEC EDGAR, technicals, news, FRED
├── strategy/    → Momentum, Mean Reversion, Whale Follow
├── analysis/    → Claude AI (2-tier), Signal Combiner, Screener
├── risk/        → Position Sizing, Portfolio Manager, Stop-Loss
├── execution/   → Trader, Order Manager, Telegram
├── storage/     → SQLite + Pydantic Models
├── backtest/    → Engine + HTML Report Generator
└── dashboard/   → FastAPI + Jinja2 + Tailwind
```

## Telegram Commands

- `/status` — Portfolio status
- `/positions` — Open positions
- `/history` — Recent trades
- `/pause` — Pause trading
- `/resume` — Resume trading
- `/kill` — Close all positions

## API Keys Needed

- **Alpaca** (free): https://alpaca.markets
- **Anthropic**: https://console.anthropic.com
- **FRED** (free): https://fred.stlouisfed.org/docs/api/api_key.html
- **Telegram Bot**: https://t.me/BotFather

## Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| MAX_POSITION_PCT | 3% | Max single position size |
| MAX_SECTOR_PCT | 40% | Max sector exposure |
| MAX_POSITIONS | 15 | Max concurrent positions |
| DAILY_DRAWDOWN_LIMIT | 2% | Stops trading for the day |
| WEEKLY_DRAWDOWN_LIMIT | 5% | Pauses bot entirely |
| MIN_SIGNAL_SCORE | 0.6 | Minimum combined signal score |
