# AlgoTrader

Automated trading support platform built with `Next.js` on the frontend and `FastAPI` on the backend.

## Stack

- `frontend/`: Next.js App Router dashboard for live signal review, backtesting, and guarded execution commands
- `backend/`: FastAPI API for market data, indicator scoring, backtesting, and broker execution
- Recommended data provider: `Alpha Vantage` for free stock market prototyping
- Recommended broker for execution: `Alpaca` paper trading

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

The frontend expects the backend at `http://127.0.0.1:8000` by default. Override it with `NEXT_PUBLIC_API_BASE_URL`.

## Environment

Copy `backend/.env.example` to `backend/.env` before using live integrations.

## Notes

- Execution defaults to simulated mode.
- Live order routing requires Alpaca credentials and `ENABLE_LIVE_EXECUTION=true`.
- The existing `client/` and `server/` folders are legacy code from the previous app and are not used by the new scaffold.
