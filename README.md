# Polymarket Copytrading Bot

A production-grade async Python bot that monitors top Polymarket traders, evaluates signals through pluggable strategies, and executes copy trades via the CLOB API — all controlled through Telegram.

## Features

- 🔍 **Trader Discovery** — Automated scoring and selection of top Polymarket traders using a composite formula (consistency, Sharpe ratio, diversity, frequency, recency)
- 📡 **Real-time Monitoring** — Polls 10+ active traders every 30s for new trades
- 🎯 **5 Strategies** — Pure Follow, Consensus Gate, Whale Entry, Category Expert, Smart Exit
- 🛡️ **Risk Management** — Position sizing, exposure caps, stop losses, daily drawdown limits
- 🤖 **Telegram Control** — Full bot control: mode switching, budget, strategy selection, reports
- 📊 **6-Hour Reports** — Automated P&L summaries with strategy performance tracking
- 🐳 **Docker Ready** — Complete docker-compose setup with PostgreSQL 16

## Quick Start

### 1. Clone and configure

```bash
cp .env.example .env
# Fill in your credentials in .env
```

### 2. Run with Docker Compose

```bash
docker-compose up -d
```

### 3. Talk to your bot

Start a conversation with your bot on Telegram and type `/start`.

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| `RELAYER_API_KEY` | Polymarket CLOB relayer API key |
| `RELAYER_API_ADDRESS` | Relayer wallet address |
| `SIGNER_ADDRESS` | Signing wallet address |
| `DATABASE_URL` | PostgreSQL connection string |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_ADMIN_ID` | Your Telegram user ID |

## Bot Commands

| Command | Description |
|---------|-------------|
| `/status` | Balance, positions, today P&L |
| `/mode auto\|manual\|paper` | Switch trading mode |
| `/pause` / `/resume` | Pause or resume auto-trading |
| `/traders` | Paginated list of tracked traders |
| `/positions` | Open positions with unrealized P&L |
| `/history [n]` | Last N closed positions |
| `/strategy list` | All strategies with 7d P&L |
| `/strategy use <slug>` | Switch active strategy |
| `/budget <usd>` | Set total budget |
| `/pertrade <pct>` | Set per-trade budget % |
| `/maxtrade <usd>` | Set max trade size |
| `/report` | Generate full report now |
| `/settings` | Show all settings |
| `/signal <address>` | Last 5 signals from trader |
| `/help` | Full command list |

## Strategies

| Slug | Name | Description |
|------|------|-------------|
| `pure_follow` | Pure Follow | Copy every trade. Exit when trader exits. |
| `consensus` | Consensus Gate | Only trade when 2+ traders agree (default: active) |
| `whale` | Whale Entry | Only copy bets > 2x trader's average size |
| `category_expert` | Category Expert | Copy only within trader's strongest category |
| `smart_exit` | Smart Exit | Enter always, exit on trailing stop / take-profit |

## Architecture

```
main.py → scheduler.py → core/monitor.py → core/executor.py
                       ↓                  ↓
               core/discovery.py   core/strategies/
                       ↓                  ↓
               api/data_api.py     api/clob.py
               api/gamma.py
                       ↓
               db/ (PostgreSQL via SQLAlchemy 2.0 async)
```

## Risk Controls

- **Total exposure cap**: Max 60% of balance deployed at once
- **Per-market cap**: Max 20% of balance in one market
- **Stop loss**: Auto-close if position loses >35%
- **Daily drawdown**: Pause if down >15% in a day
- **Spread filter**: Skip markets with bid-ask spread >10%
- **Staleness filter**: Skip trades with price data >15min old
- **Resolution filter**: Skip markets resolving within 48h

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations manually
alembic upgrade head

# Run locally (set DATABASE_URL to local postgres)
python main.py
```
