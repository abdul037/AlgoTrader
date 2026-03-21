# AlgoTrader

Automated trading support platform built with `Next.js` on the frontend and `FastAPI` on the backend.

## Stack

- `frontend/`: Next.js App Router dashboard for live signal review and backtesting
- `backend/`: FastAPI API for market data, indicator scoring, and backtesting
- Supported data providers: `demo`, `Alpha Vantage`, and `Alpaca`
- Workflow: signal-only and manual trade decisions

## Quick Start

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload
```

### Run Together

From the repo root, one terminal is enough once dependencies are installed:

```bash
npm install
npm run dev
```

If you want backend auto-reload while editing Python files, run `npm run dev:backend:reload` separately.

Single-service commands:

```bash
npm run dev:frontend
npm run dev:backend
```

Build checks:

```bash
npm run build
```

The frontend expects the backend at `http://127.0.0.1:8000` by default. Override it with `NEXT_PUBLIC_API_BASE_URL`.

## Environment

Copy `backend/.env.example` to `backend/.env` before using live data integrations.

Example `backend/.env` for Alpaca market data:

```env
MARKET_DATA_PROVIDER=alpaca
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_DATA_FEED=iex
```

Use `ALPACA_DATA_FEED=sip` only if your Alpaca subscription supports it.

## Notes

- Execution has been removed from this build.
- The app now focuses on buy/sell/hold signals and strategy backtesting only.
- The existing `client/` and `server/` folders are legacy code from the previous app and are not used by the new scaffold.
