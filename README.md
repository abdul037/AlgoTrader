# eToro Approval Trading Bot

Production-minded Python trading system scaffold for backtesting, multi-stock screening, live signal generation, Telegram alerting, mandatory approval gates by default, and a safe-first eToro broker integration.

## Current Architecture

The codebase now has four primary layers:

- market data and caching: broker-aligned eToro market data plus Yahoo fallback through a shared market-data engine
- strategy and signal generation: reusable strategy modules for swing, intraday, breakout, trend-following, mean-reversion, RSI, VWAP, EMA, and confluence scans across `1m`, `5m`, `15m`, `1h`, and `1d`
- backtesting and validation: single-run and batch backtests stored in SQLite, reused to gate live alerts, and compared with paper-trading outcomes
- delivery and approval: Telegram alerts, webhook command handling, proposal approval flow, execution queue handling, paper trading, and a future-facing execution interface

The active production Telegram path is webhook mode through FastAPI. Polling remains as a fallback/debug path.

## Safe Defaults

- `ETORO_ACCOUNT_MODE=demo` by default.
- `ENABLE_REAL_TRADING=false` by default.
- `REQUIRE_APPROVAL=true` by default.
- `EXECUTION_MODE=paper` by default.
- Unsupported and explicitly blocked instruments are rejected before proposal creation.
- Risk validation runs before proposal creation and again before execution.
- Orders without stop losses are rejected.

## Initial Scope

Supported instruments:

- `NVDA`
- `GOOG`
- `GOOGL`
- `AMD`
- `MU`
- `GOLD`

Blocked by default:

- `OIL`
- `NATGAS`
- `SILVER`
- any unsupported symbol

Leverage caps:

- equities: `1x` to `5x`
- gold: up to `10x`

## Project Tree

```text
etoro-bot/
  README.md
  .env.example
  requirements.txt
  pyproject.toml
  app/
    __init__.py
    main.py
    config.py
    logging_config.py
    models/
      __init__.py
      trade.py
      signal.py
      approval.py
      execution.py
    broker/
      __init__.py
      etoro_client.py
      instrument_resolver.py
    data/
      __init__.py
      market_data.py
      csv_loader.py
    strategies/
      __init__.py
      base.py
      ma_crossover.py
      pullback_trend.py
      gold_momentum.py
    backtesting/
      __init__.py
      engine.py
      metrics.py
    risk/
      __init__.py
      rules.py
      position_sizing.py
      guardrails.py
    approvals/
      __init__.py
      service.py
      routes.py
    execution/
      __init__.py
      trader.py
      scheduler.py
    storage/
      __init__.py
      db.py
      repositories.py
    utils/
      __init__.py
      time.py
      ids.py
  scripts/
    seed_demo_data.py
    run_backtest.py
    propose_trade.py
  sample_data/
    nvda.csv
  tests/
    conftest.py
    test_backtest.py
    test_risk_rules.py
    test_approval_flow.py
    test_strategy_signals.py
```

## Local Setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Copy `.env.example` to `.env`.
4. Keep demo mode enabled until the eToro account-specific API contract is verified.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Keep `.env.example` as placeholders only. Put real secrets in `.env`, and do not commit that file.

## Configuration

Key environment variables:

```env
ETORO_API_KEY=
ETORO_USER_KEY=
ETORO_BASE_URL=https://public-api.etoro.com
ETORO_ACCOUNT_MODE=demo
ENABLE_REAL_TRADING=false
REQUIRE_APPROVAL=true
MAX_RISK_PER_TRADE_PCT=1.0
MAX_DAILY_LOSS_USD=100
MAX_OPEN_POSITIONS=3
ALLOWED_INSTRUMENTS=NVDA,GOOG,GOOGL,AMD,MU,GOLD
BLOCKED_INSTRUMENTS=OIL,NATGAS,SILVER
DEFAULT_EQUITY_LEVERAGE=1
MAX_EQUITY_LEVERAGE=5
MAX_GOLD_LEVERAGE=10
```

Review-only exception note:

- `CRWV` is not in the default allowlist.
- If you want to evaluate a one-off manually reviewed `CRWV` trade, add it temporarily to `ALLOWED_INSTRUMENTS` in your live env only after you decide to review that symbol explicitly.
- Keep `REQUIRE_APPROVAL=true` and `ENABLE_REAL_TRADING=false` until you intentionally promote that proposal to a real approved trade.

Authentication notes from the official eToro Public API docs:

- `ETORO_API_KEY` is the public app API key from the developer portal.
- `ETORO_USER_KEY` is the generated secret key from eToro API Key Management. In the docs it is sent as `x-user-key`, while the UI shows it under `Generated Keys`.
- `ETORO_GENERATED_KEY` is also accepted by this app as an alias for `ETORO_USER_KEY` if you want your `.env` to match the eToro UI wording.

Signal validation notes:

- Live Telegram and API signal alerts can be gated by stored backtest quality thresholds.
- When `REQUIRE_BACKTEST_VALIDATION_FOR_ALERTS=true`, alerts are suppressed unless the latest stored backtest for the symbol/strategy passes the configured thresholds.
- Relevant env settings:
  - `MIN_BACKTEST_TRADES_FOR_ALERTS`
  - `MIN_BACKTEST_PROFIT_FACTOR`
  - `MIN_BACKTEST_ANNUALIZED_RETURN_PCT`
  - `MAX_BACKTEST_DRAWDOWN_PCT`
- Telegram alerts now include source and validation context so you can distinguish live verified broker data from fallback or unvalidated signals.
- The generated key is environment-specific, so use a Demo generated key when `ETORO_ACCOUNT_MODE=demo`.
- `x-request-id` is also required by the API, but it should be generated as a fresh UUID per request rather than stored in `.env`.

Scanner and ranking notes:

- Market-data provenance can hard-gate alerts:
  - `REQUIRE_VERIFIED_MARKET_DATA_FOR_ALERTS`
  - `REQUIRE_PRIMARY_PROVIDER_FOR_ALERTS`
  - `REQUIRE_DIRECT_QUOTE_FOR_ALERTS`
  - `REQUIRE_UNCACHED_MARKET_DATA_FOR_ALERTS`
  - `MAX_MARKET_DATA_AGE_SECONDS`
- Named market-phase schedules are configurable with:
  - `SCHEDULE_TIMEZONE`
  - `PREMARKET_SCAN_TIME_LOCAL`
  - `MARKET_OPEN_SCAN_TIME_LOCAL`
  - `INTRADAY_SCAN_START_LOCAL`
  - `INTRADAY_SCAN_END_LOCAL`
  - `END_OF_DAY_SCAN_TIME_LOCAL`
- Alert overlap is prevented with workflow lock keys. Configure stale-lock expiry with `WORKFLOW_LOCK_TIMEOUT_MINUTES`.
- The live screener now applies configurable filters for price sanity, volume, dollar volume, spread, ATR, trend strength, choppiness, reward-to-risk, entry-location accuracy, confirmation strength, false-positive risk, and regime alignment.
- Repeat alerts are suppressed unless the score improves materially, controlled by:
  - `SCREENER_DUPLICATE_ALERT_WINDOW_MINUTES`
  - `SCREENER_MIN_SCORE_IMPROVEMENT_FOR_REPEAT`
- Weak backtest profiles can be blocked, downgraded to watchlist, or only rank-penalized via `SCREENER_WEAK_BACKTEST_ACTION`.
- The final ranked alert score blends live setup quality, liquidity, volatility, reward-to-risk, backtest win rate, profit factor, sample size, recent consistency, and regime alignment into a normalized `0-100` score.
- Historical bars may come from cache if they are fresh enough. Direct eToro quote verification remains mandatory for alertable signals in tradable mode.
- The new intelligence layer also evaluates timeframe alignment, market regime, execution quality, and indicator confluence before a signal is marked execution-ready.

Tradable-mode additions:

```env
PRIMARY_MARKET_DATA_PROVIDER=auto
FALLBACK_MARKET_DATA_PROVIDER=none
REQUIRE_VERIFIED_MARKET_DATA_FOR_ALERTS=true
REQUIRE_PRIMARY_PROVIDER_FOR_ALERTS=true
REQUIRE_DIRECT_QUOTE_FOR_ALERTS=true
REQUIRE_UNCACHED_MARKET_DATA_FOR_ALERTS=false
MAX_MARKET_DATA_AGE_SECONDS=120
SCREENER_DEFAULT_TIMEFRAMES=15m,1h,1d
SCREENER_INTRADAY_TIMEFRAMES=1m,5m,15m
INTELLIGENT_SCAN_TIMEFRAMES=5m,15m,1h,1d
SINGLE_SYMBOL_ANALYSIS_TIMEFRAMES=1m,5m,15m,1h,1d
INTELLIGENT_SCAN_ENABLED=true
INTELLIGENT_SCAN_START_LOCAL=09:45
INTELLIGENT_SCAN_END_LOCAL=15:45
INTELLIGENT_SCAN_INTERVAL_MINUTES=120
SCREENER_MIN_INDICATOR_CONFLUENCE=0.45
SCREENER_MIN_EXECUTION_QUALITY=0.5
SCREENER_MIN_ACCURACY_SCORE=0.52
SCREENER_MIN_CONFIRMATION_SCORE=0.45
SCREENER_MAX_FALSE_POSITIVE_RISK=0.68
SCREENER_MIN_RESISTANCE_ATR_DISTANCE=0.35
SCREENER_MAX_LATE_ENTRY_ATR_MULTIPLE=2.4
SCREENER_SCALP_MIN_CONFIDENCE=0.72
SCREENER_SCALP_MIN_RELATIVE_VOLUME=1.25
SCREENER_SCALP_MAX_SPREAD_BPS=18
EXECUTION_MODE=paper
EXECUTION_QUEUE_ENABLED=true
EXECUTION_RECHECK_QUOTE_BEFORE_ORDER=true
EXECUTION_MAX_ENTRY_DRIFT_BPS=35
PAPER_TRADING_ENABLED=true
PAPER_ACCOUNT_BALANCE_USD=100000
MAX_WEEKLY_LOSS_USD=300
PER_SYMBOL_POSITION_LIMIT=1
MAX_CONSECUTIVE_LOSSES_BEFORE_COOLDOWN=2
KILL_SWITCH_ENABLED=false
```

Allowed instruments and leverage rules live in the settings layer and are enforced by [`app/config.py`](/Users/abdul/Projects/CX_AlgoBot/app/config.py) and [`app/risk/guardrails.py`](/Users/abdul/Projects/CX_AlgoBot/app/risk/guardrails.py).

## Run Tests

```bash
python3 -m pytest -q
```

Current local verification result:

- focused screener/workflow/Telegram suite: `10 passed`

## Run a Sample Backtest

Synthetic NVDA data is already seeded in [`sample_data/nvda.csv`](/Users/abdul/Projects/CX_AlgoBot/sample_data/nvda.csv).

```bash
python3 scripts/run_backtest.py --symbol NVDA --strategy ma_crossover --file sample_data/nvda.csv
```

You can regenerate the synthetic CSV if needed:

```bash
python3 scripts/seed_demo_data.py
```

## Start the API Server

```bash
uvicorn app.main:app --reload
```

Swagger/OpenAPI UI:

- `http://127.0.0.1:8000/docs`

Core endpoints:

- `GET /health`
- `POST /backtests/run`
- `GET /screener/universe`
- `GET /screener/scan`
- `GET /screener/analyze`
- `POST /screener/backtests/run`
- `GET /workflow/status`
- `GET /workflow/scan-decisions`
- `POST /workflow/run/premarket-scan`
- `POST /workflow/run/market-open-scan`
- `POST /workflow/run/intelligent-scan`
- `POST /workflow/run/intraday-scan`
- `POST /workflow/run/end-of-day-scan`
- `POST /proposals/create`
- `GET /proposals`
- `POST /proposals/{id}/approve`
- `POST /proposals/{id}/reject`
- `POST /proposals/{id}/execute`
- `GET /execution/queue`
- `POST /execution/queue/{proposal_id}/enqueue`
- `POST /execution/queue/process`
- `GET /paper/summary`
- `GET /paper/positions`
- `GET /paper/trades`
- `POST /paper/refresh`
- `GET /portfolio/summary`
- `GET /config/summary`

## Approval Workflow

Default execution flow:

1. strategy generates a signal
2. risk validation runs
3. proposal is stored as `pending`
4. human approves or rejects
5. approved proposals are placed onto the execution queue
6. execution re-checks risk, price freshness, and entry drift before paper or live placement

Example: create a proposal manually through the API

```bash
curl -X POST http://127.0.0.1:8000/proposals/create \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "NVDA",
    "amount_usd": 1000,
    "leverage": 1,
    "proposed_price": 120.0,
    "stop_loss": 114.0,
    "take_profit": 132.0,
    "strategy_name": "ma_crossover",
    "rationale": "Trend resumed after pullback.",
    "notes": "manual demo proposal"
  }'
```

Approve it:

```bash
curl -X POST http://127.0.0.1:8000/proposals/<PROPOSAL_ID>/approve \
  -H "Content-Type: application/json" \
  -d '{
    "reviewer": "human",
    "notes": "approved for demo"
  }'
```

Execute it:

```bash
curl -X POST http://127.0.0.1:8000/proposals/<PROPOSAL_ID>/execute
```

You can also propose from a strategy signal using the script:

```bash
python3 scripts/propose_trade.py --symbol NVDA --strategy ma_crossover --file sample_data/nvda.csv
```

## Demo vs Real Mode

Demo behavior in this version:

- demo mode is the default account mode
- broker calls use a safe simulation path unless verified eToro credentials and endpoints are configured
- real trading requests are blocked unless `ENABLE_REAL_TRADING=true`

The eToro integration in [`app/broker/etoro_client.py`](/Users/abdul/Projects/CX_AlgoBot/app/broker/etoro_client.py) isolates endpoint uncertainty behind the broker client and includes explicit `TODO` markers where account-specific payload verification is still required.

## Warnings

- This repository is a safe-first scaffold, not a fully verified production deployment.
- Real capital should not be used until eToro endpoint contracts, payloads, authentication, order semantics, and fill handling are verified against the target account.
- Backtest performance on synthetic or historical data does not guarantee live performance.
- Keep `ENABLE_REAL_TRADING=false` until you have completed a controlled demo validation cycle.
## Research-Only Strategy Analysis

To study a real historical signal without sending any order, run:

```bash
python3 scripts/research_nvda.py --symbol NVDA --period 5y --interval 1d
```

This produces a markdown report at `reports/nvda_strategy_report.md` with:

- bars analyzed
- indicator families and parameter sets tested
- parameter sweep metrics
- recommended entry and exit framework
- latest signal status

This path is analysis-only and does not place, cancel, or modify broker orders.

## Live Signals And Market Screener

The app can evaluate single-symbol live signals and run ranked multi-stock universe scans without sending any order.

Endpoints:

- `GET /signals/latest?symbol=NVDA`
- `GET /signals/scan?limit=20`
- `GET /screener/universe`
- `GET /screener/scan?limit=20`
- `GET /screener/analyze?symbol=NVDA`
- `POST /screener/backtests/run`
- `POST /workflow/run/intelligent-scan`
- `POST /workflow/run/ledger-cycle`
- `POST /signals/test-telegram`
- `POST /signals/notify`

Examples:

```bash
curl "http://127.0.0.1:8000/signals/latest?symbol=NVDA"
curl "http://127.0.0.1:8000/signals/scan?limit=20"
curl "http://127.0.0.1:8000/signals/scan?limit=20&notify=true"
curl "http://127.0.0.1:8000/screener/universe"
curl "http://127.0.0.1:8000/screener/scan?limit=10&validated_only=true"
curl "http://127.0.0.1:8000/screener/analyze?symbol=NVDA"
curl -X POST "http://127.0.0.1:8000/workflow/run/intelligent-scan"
curl -X POST "http://127.0.0.1:8000/workflow/run/ledger-cycle"
curl -X POST "http://127.0.0.1:8000/screener/backtests/run" -H "Content-Type: application/json" -d '{"timeframes":["1d","1h"],"limit":25}'
curl -X POST "http://127.0.0.1:8000/signals/test-telegram" -H "Content-Type: application/json" -d '{"message":"manual test"}'
curl -X POST "http://127.0.0.1:8000/signals/notify" -H "Content-Type: application/json" -d '{"symbol":"NVDA"}'
python3 scripts/check_live_signals.py --symbol NVDA
python3 scripts/check_live_signals.py --scan --limit 20 --notify
python3 scripts/run_market_scan.py --limit 10 --validated-only --notify
python3 scripts/run_universe_backtests.py --timeframes 1d,1h --limit 25
python3 scripts/run_ledger_cycle.py --summary --recent 20
```

Notes:

- `GET /signals/latest` evaluates the latest closed daily bar from eToro candles and returns entry, exit, stop, target, and indicator context.
- `GET /signals/scan` ranks the configured symbol universe and returns the top candidates by live setup score.
- `GET /screener/scan` runs the new configurable market-universe screener across multiple timeframes and strategies.
- `GET /screener/analyze` returns premium single-symbol intelligence, including trade-plan guidance, market regime context, and execution-readiness details.
- `POST /workflow/run/intelligent-scan` runs the 2-hour market-aware scan path over the configured US-stock universe.
- `POST /workflow/run/ledger-cycle` snapshots the real eToro portfolio, matches new positions to bot alerts, detects closures, and updates the signal outcome ledger.
- `POST /screener/backtests/run` batch-tests the configured universe and stores summaries for later alert validation.
- `POST /signals/test-telegram` sends a manual test message to the configured Telegram chat.
- `POST /signals/notify` forces the current signal snapshot for one symbol to be sent to Telegram even if the state has not changed.
- The scanner can include symbols outside the trading allow-list for research. Each result includes whether it is currently supported by the bot for execution.
- Successfully delivered scheduled screener alerts are auto-recorded into the outcome ledger when `LEDGER_RECORD_ALERTS_ENABLED=true`.
- `notify=true` sends Telegram alerts only when the signal state changes for a symbol.
- This feature is signal and watchlist generation only. It does not bypass the approval flow and does not submit trades.

## Telegram Commands

Webhook mode is the primary production path. Keep the API server and public HTTPS tunnel running, then send commands directly from Telegram.

Supported commands in Telegram:

- `/start`
- `/help`
- `/signal NVDA`
- `/price AMD`
- `/scan 5`
- `/intraday_scan 5`
- `/supported_scan 10`
- `/validated_scan 10`
- `/propose NVDA 20`
- `/propose_top 20`
- `/propose_top 20 25` scans only the first 25 universe symbols for a faster test
- `/proposals`
- `/approve PROPOSAL_ID`
- `/reject PROPOSAL_ID`
- `/enqueue PROPOSAL_ID`
- `/queue`
- `/process_queue QUEUE_ID`
- `/open_signals`
- `/daily_summary`
- `/notify NVDA`

Approval command notes:

- `/propose` creates a pending proposal only when the live signal is execution-ready and has a stop/target trade plan.
- `/propose_top` scans the active market universe, selects the top execution-ready candidate, and creates a pending proposal for that candidate.
- `/propose` refuses `NO_TRADE` setups; it will not force a trade just because an amount is provided.
- `/approve` only approves. Use `/enqueue` and `/process_queue` for the explicit execution step.
- `/process_queue` follows `EXECUTION_MODE`; default `paper` mode simulates the trade.
- If `EXECUTION_MODE=live`, `/process_queue` requires `CONFIRM_LIVE` as an extra argument and real trading must still be enabled in config.

Fallback polling mode still exists for local debugging:

```bash
python3 scripts/run_telegram_bot.py
```

Scheduling:

- The webhook-backed app or polling runner can send hourly Telegram alerts for configured symbols.
- Default interval is `60` minutes.
- Default hourly alert symbol is `NVDA`.
- Configure these with `TELEGRAM_HOURLY_ALERTS_ENABLED`, `TELEGRAM_ALERT_INTERVAL_MINUTES`, and `TELEGRAM_ALERT_SYMBOLS`.

Example signal alert format:

```text
Signal change for NVDA
Strategy: momentum_breakout
Timeframe: 1D
Source: etoro
Verified: yes
State: query -> buy
Price: 918.4
Entry: 920.1
Exit: 963.8
Stop: 898.6
Target: 963.8
Confidence: 72.0%
RR: 2.3
Score: 104.40
Backtest validated: yes
Backtest PF: 1.84
Backtest ann. return %: 16.5
```

## Telegram Webhook Mode

Run the API server:

```bash
uvicorn app.main:app --reload
```

Set these environment values:

```env
TELEGRAM_ENABLED=true
TELEGRAM_WEBHOOK_URL=https://your-public-host/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=choose-a-random-secret
TELEGRAM_HOURLY_ALERTS_ENABLED=true
TELEGRAM_ALERT_INTERVAL_MINUTES=60
TELEGRAM_ALERT_SYMBOLS=NVDA
```

Register the webhook:

```bash
python3 scripts/register_telegram_webhook.py --url https://your-public-host/telegram/webhook
```

Or use the API directly:

```bash
curl -X POST http://127.0.0.1:8000/telegram/webhook/register \
  -H "Content-Type: application/json" \
  -d '{"webhook_url":"https://your-public-host/telegram/webhook"}'
```

Check the webhook:

```bash
curl http://127.0.0.1:8000/telegram/webhook/status
```

With webhook mode, Telegram commands such as `/signal NVDA`, `/price AMD`, `/scan 5`, and `/notify NVDA` are handled by FastAPI instead of the polling runner.

## Universe And Strategy Configuration

The market screener is configurable through env settings:

```env
MARKET_UNIVERSE_NAME=top100_us
MARKET_UNIVERSE_LIMIT=100
MARKET_UNIVERSE_SYMBOLS=
PRIMARY_MARKET_DATA_PROVIDER=auto
FALLBACK_MARKET_DATA_PROVIDER=yfinance
SCREENER_DEFAULT_TIMEFRAMES=1d,1h,15m
SCREENER_INTRADAY_TIMEFRAMES=15m,1h
SCREENER_ACTIVE_STRATEGY_NAMES=rsi_vwap_ema_confluence
SCREENER_PRIMARY_STRATEGY_NAME=rsi_vwap_ema_confluence
SCREENER_TOP_K=20
SCREENER_MIN_CONFIDENCE=0.45
REQUIRE_BACKTEST_VALIDATION_FOR_ALERTS=true
MIN_BACKTEST_TRADES_FOR_ALERTS=10
MIN_BACKTEST_PROFIT_FACTOR=1.2
MIN_BACKTEST_ANNUALIZED_RETURN_PCT=5
MAX_BACKTEST_DRAWDOWN_PCT=35
```

Current strategy catalog:

- `pullback_trend`
- `ma_crossover`
- `trend_following`
- `momentum_breakout`
- `mean_reversion`
- `intraday_vwap_trend`
- `ema_trend_stack`
- `rsi_trend_continuation`
- `rsi_reversal`
- `vwap_reclaim`
- `rsi_vwap_ema_confluence`

Default alert/proposal mode is intentionally narrower than the full catalog:

- `SCREENER_ACTIVE_STRATEGY_NAMES=rsi_vwap_ema_confluence` makes RSI + VWAP + EMA confluence the only live alert/proposal strategy.
- Set `SCREENER_ACTIVE_STRATEGY_NAMES=all` only for research or broad comparative backtests.
- The confluence strategy is available on `1m`, `5m`, `15m`, `1h`, and `1d`.
- Other strategies remain available for explicit backtest requests and future comparison, but they do not produce live alerts unless enabled.

Primary confluence controls:

```env
CONFLUENCE_MINIMUM_SCORE=0.84
CONFLUENCE_MINIMUM_RELATIVE_VOLUME=1.25
CONFLUENCE_MINIMUM_ADX=20
CONFLUENCE_RSI_LONG_MIN=54
CONFLUENCE_RSI_LONG_MAX=66
CONFLUENCE_RSI_SHORT_MIN=34
CONFLUENCE_RSI_SHORT_MAX=46
CONFLUENCE_MAX_EXTENSION_ATR=1.6
CONFLUENCE_MIN_BODY_TO_RANGE=0.32
CONFLUENCE_MIN_CLOSE_LOCATION=0.62
```

These gates intentionally reduce signal frequency. The objective is not a 99% win-rate claim; it is fewer A+ setups with controlled risk, better reward-to-risk, and cleaner forward-test evidence.

## Future Execution Layer

Direct broker execution should remain behind the approval flow. The current design keeps this cleanly separated:

- signal generation and ranking stay in the screener/signal services
- Telegram communicates opportunities and approval context
- proposal creation and review live in the approvals layer
- broker placement can later be implemented behind [`app/execution/interfaces.py`](/Users/abdul/Projects/CX_AlgoBot/app/execution/interfaces.py)

That keeps the execution boundary explicit and makes it practical to add eToro order placement later without coupling broker logic to research and scanning code.

## Workflow Automation

The app now has a workflow layer on top of the screener:

- scheduled swing scans
- scheduled intraday scans
- tracked open signal monitoring
- stop-hit / target-hit notifications
- daily summary generation
- alert history persistence

New API endpoints:

- `GET /workflow/status`
- `GET /workflow/tracked-signals`
- `GET /workflow/alerts`
- `POST /workflow/run/swing-scan`
- `POST /workflow/run/intraday-scan`
- `POST /workflow/run/open-signal-check`
- `POST /workflow/run/daily-summary`

Manual CLI runner:

```bash
python3 scripts/run_workflow_cycle.py --task swing --no-notify
python3 scripts/run_workflow_cycle.py --task intraday --no-notify
python3 scripts/run_workflow_cycle.py --task open-check --no-notify
python3 scripts/run_workflow_cycle.py --task daily-summary --no-notify
```

Telegram commands added for workflow monitoring:

- `/open_signals`
- `/daily_summary`

Workflow env settings:

```env
SCREENER_SCHEDULER_ENABLED=true
SCHEDULE_TIMEZONE=America/New_York
PREMARKET_SCAN_TIME_LOCAL=08:30
MARKET_OPEN_SCAN_TIME_LOCAL=09:35
INTRADAY_SCAN_START_LOCAL=10:00
INTRADAY_SCAN_END_LOCAL=15:30
END_OF_DAY_SCAN_TIME_LOCAL=15:50
WORKFLOW_LOCK_TIMEOUT_MINUTES=45
INTRADAY_SCAN_INTERVAL_MINUTES=15
OPEN_SIGNAL_CHECK_INTERVAL_MINUTES=5
DAILY_SUMMARY_HOUR_UTC=20
TRACK_ALERTED_SIGNALS=true
SCREENER_ALERT_MODE=digest
SCREENER_TOP_ALERTS_PER_RUN=5
SCREENER_MIN_FINAL_SCORE_TO_ALERT=65
SCREENER_DUPLICATE_ALERT_WINDOW_MINUTES=240
SCREENER_MIN_SCORE_IMPROVEMENT_FOR_REPEAT=6
SCREENER_WEAK_BACKTEST_ACTION=watchlist
```

Operational note:

- webhook mode is the primary production path
- polling mode remains available as a fallback/debug path
- if you use webhook mode, keep `TELEGRAM_POLLING_ENABLED=false`

The workflow now persists structured `scan_decisions` records for each evaluated live setup. Each record includes:

- scan task
- symbol / strategy / timeframe
- pass / reject / suppressed / watchlist status
- final score
- freshness
- reason codes
- rejection reasons
- serialized candidate payload for later review

## Ranking And Alert Output

The live screener now ranks candidates with a weighted `0-100` score across:

- setup quality
- trend strength
- momentum confirmation
- liquidity quality
- volatility suitability
- reward-to-risk quality
- entry-location accuracy
- support/resistance room
- confirmation strength
- false-positive risk
- backtest win rate
- backtest profit factor
- backtest sample size credibility
- recent backtest consistency
- regime alignment

Only candidates above `SCREENER_MIN_FINAL_SCORE_TO_ALERT` are alert-eligible. Weak backtest profiles can be blocked, downgraded to `watchlist`, or only rank-penalized based on `SCREENER_WEAK_BACKTEST_ACTION`.

Accuracy controls are intentionally separate from strategy generation:

- `SCREENER_MIN_ACCURACY_SCORE` blocks setups with poor entry location or weak room to target.
- `SCREENER_MIN_CONFIRMATION_SCORE` requires enough RSI/VWAP/EMA/MACD/RVOL/ADX agreement.
- `SCREENER_MAX_FALSE_POSITIVE_RISK` rejects choppy, exhausted, low-volume, or late-entry structures.
- `SCREENER_MAX_LATE_ENTRY_ATR_MULTIPLE` prevents chasing entries too far from EMA/VWAP anchors.

Example ranked Telegram card:

```text
US market screener
Run: market_open_scan
Universe: top100_us
Timeframes: 15m, 1h
Scanned: 100 symbols | Strategy runs: 400 | Passed: 3 | Suppressed: 9

#1 NVDA | BUY
Setup: momentum_breakout | 1h | Score 84.7/100 | strong
Freshness: fresh
Entry: 124.20 | Stop: 119.40 | Targets: 132.30
RR: 1.70 | Price: 124.05 | Time: 2026-04-13T14:35:00+00:00
Indicators: RSI 61.40 | VWAP 121.80 | EMA9 123.10 | EMA20 121.90 | RVOL 1.80 | ADX 24.50 | Confluence 0.76 | Accuracy 0.74 | Confirm 0.71 | FP-risk 0.24
Backtest: WR 56.0% | PF 1.82 | Trades 28 | Exp 1.14% | DD 16.0%
Why: confidence_ok, dollar_volume_ok, volatility_ok, regime_alignment_ok, backtest_validated
Execution: manual approval required before any broker action.
```

## Live Quote Verification

Alert eligibility is now split into three explicit checks:

- live quote verification: the latest quote must be direct from eToro, primary-provider backed, and younger than `MAX_MARKET_DATA_AGE_SECONDS`
- historical bar freshness: cached bars are allowed only if they are fresh enough for the configured age threshold
- strategy and execution readiness: score, reward-to-risk, confluence, regime fit, and execution blockers must all pass

Every premium symbol analysis and screener candidate now exposes:

- `data_source`
- `data_source_history`
- `quote_live_verified`
- `quote_timestamp`
- `bar_timestamp`
- `freshness_status`
- `execution_ready`
- `execution_blockers`

That keeps “good research context” separate from “allowed to alert now”.

## Signal Outcome Ledger

The ledger closes the feedback loop between Telegram alerts and real eToro account outcomes:

- scheduled alertable candidates are recorded as `pending_match`
- the ledger cycle snapshots the real eToro portfolio
- newly opened matching positions become `open`
- disappeared positions are classified as `target_hit`, `stop_hit`, or `closed_manual`
- realized PnL and R-multiple are stored for later signal-quality calibration

Ledger controls:

```env
LEDGER_ENABLED=true
LEDGER_RECORD_ALERTS_ENABLED=true
LEDGER_CYCLE_ENABLED=true
LEDGER_CYCLE_INTERVAL_MINUTES=15
LEDGER_MATCH_WINDOW_MINUTES=120
LEDGER_PENDING_EXPIRY_HOURS=48
LEDGER_TRACK_MANUAL_POSITIONS_ENABLED=false
```

Manual check:

```bash
python3 scripts/run_ledger_cycle.py --summary --recent 20
curl -X POST "http://127.0.0.1:8000/workflow/run/ledger-cycle"
```

Manual eToro transaction tracking:

- Set `LEDGER_TRACK_MANUAL_POSITIONS_ENABLED=true` to auto-import newly opened manual eToro positions.
- The ledger imports only positions that appear after the previous snapshot, so existing account positions are treated as the baseline.
- Imported manual positions use `alert_source=manual_etoro` and `strategy_name=manual_etoro`, keeping them separate from bot-generated signal results.
- This is read-only account observation; it does not place, close, or modify eToro trades.

## Paper Trading And Execution Queue

The execution path is now staged:

1. a signal or proposal is approved
2. `/execution/queue/{proposal_id}/enqueue` places it in the execution queue
3. the coordinator re-checks quote freshness, provider provenance, and entry drift
4. in `EXECUTION_MODE=paper`, the position is simulated and tracked in SQLite
5. in `EXECUTION_MODE=live`, the same queue path can call the broker trader once the eToro execution contract is verified

Paper routes:

- `GET /paper/summary`
- `GET /paper/positions`
- `GET /paper/trades`
- `POST /paper/refresh`

Execution routes:

- `GET /execution/queue`
- `POST /execution/queue/{proposal_id}/enqueue`
- `POST /execution/queue/{queue_id}/process`
- `POST /execution/queue/process`

Example paper summary payload:

```json
{
  "mode": "paper",
  "starting_balance": 100000.0,
  "realized_pnl_usd": 420.5,
  "unrealized_pnl_usd": 118.2,
  "open_positions": 2,
  "closed_trades": 9,
  "win_rate": 55.56,
  "expectancy_usd": 46.72
}
```

## Testing The Current System

Use the healthy working repo:

```bash
cd /Users/abdul/Projects/CX_AlgoBot
```

Run the focused verification suite:

```bash
pytest -q tests/test_screener_service.py tests/test_telegram_bot.py tests/test_workflow_service.py tests/test_approval_flow.py tests/test_risk_rules.py
```

Start the API:

```bash
uvicorn app.main:app --port 8011
```

Then test the upgraded paths:

```bash
curl http://127.0.0.1:8011/health
curl "http://127.0.0.1:8011/screener/analyze?symbol=NVDA"
curl "http://127.0.0.1:8011/screener/scan?symbols=NVDA,AMD,AAPL,MSFT&timeframes=1m,5m,15m,1h,1d&limit=5"
curl -X POST http://127.0.0.1:8011/workflow/run/intelligent-scan
curl http://127.0.0.1:8011/paper/summary
curl http://127.0.0.1:8011/execution/queue
```

Telegram smoke test:

- `/signal NVDA`
- `/signal AMD`
- `/scan 5`
- `/intraday_scan 5`
- `/validated_scan 5`
- `/open_signals`
- `/daily_summary`

## Phased Rollout

Use this rollout order:

1. paper mode: keep `EXECUTION_MODE=paper`, `ENABLE_REAL_TRADING=false`, and strict eToro quote verification on
2. approved semi-auto: keep approval mandatory and use the execution queue with fresh-quote revalidation before any placement
3. live-ready: only after broker payloads, fills, and slippage handling are verified against the target eToro account
