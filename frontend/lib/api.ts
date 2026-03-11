import { BacktestResult, SignalReport, TradingConfig } from "./types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = (await response.json()) as T & { detail?: string; error?: string };

  if (!response.ok) {
    throw new Error(payload.detail ?? payload.error ?? "Request failed.");
  }

  return payload;
}

export async function fetchTradingConfig(): Promise<TradingConfig> {
  const response = await fetch(`${API_BASE_URL}/api/trading/config`, { cache: "no-store" });
  return parseResponse<TradingConfig>(response);
}

export async function fetchTradingSignal(symbol: string, interval: string): Promise<SignalReport> {
  const response = await fetch(
    `${API_BASE_URL}/api/trading/analyze?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`,
    { cache: "no-store" }
  );

  return parseResponse<SignalReport>(response);
}

export async function runBacktest(symbol: string, interval: string): Promise<BacktestResult> {
  const response = await fetch(`${API_BASE_URL}/api/trading/backtest`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      symbol,
      interval,
      lookback: 320,
      startingCapital: 10000,
    }),
  });

  return parseResponse<BacktestResult>(response);
}
