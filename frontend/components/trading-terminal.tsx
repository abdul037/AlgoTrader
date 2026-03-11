"use client";

import { useEffect, useState } from "react";
import {
  BacktestResult,
  ExecuteTradePayload,
  ExecutionResult,
  SignalReport,
  TradingConfig,
} from "../lib/types";
import {
  fetchTradingConfig,
  fetchTradingSignal,
  postExecutionCommand,
  runBacktest,
} from "../lib/api";

const intervals = ["1min", "5min", "15min", "30min", "60min", "daily"] as const;

const formatMoney = (value: number | null | undefined) => {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
};

const formatPercent = (value: number | null | undefined) => {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  return `${value.toFixed(2)}%`;
};

const createPath = (values: number[]) => {
  if (values.length === 0) {
    return "";
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = Math.max(max - min, 1);

  return values
    .map((value, index) => {
      const x = (index / Math.max(values.length - 1, 1)) * 100;
      const y = 100 - ((value - min) / spread) * 100;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
};

export function TradingTerminal() {
  const [config, setConfig] = useState<TradingConfig | null>(null);
  const [signal, setSignal] = useState<SignalReport | null>(null);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [executionResult, setExecutionResult] = useState<ExecutionResult | null>(null);
  const [symbol, setSymbol] = useState("AAPL");
  const [interval, setInterval] = useState<(typeof intervals)[number]>("15min");
  const [loading, setLoading] = useState<"signal" | "backtest" | "execute" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [commandText, setCommandText] = useState("BUY 10 AAPL");
  const [dryRun, setDryRun] = useState(true);

  useEffect(() => {
    fetchTradingConfig()
      .then(setConfig)
      .catch((cause) => {
        setError(cause instanceof Error ? cause.message : "Could not load app configuration.");
      });
  }, []);

  useEffect(() => {
    void handleAnalyze();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleAnalyze = async () => {
    setLoading("signal");
    setError(null);

    try {
      const nextSignal = await fetchTradingSignal(symbol, interval);
      setSignal(nextSignal);
      setCommandText(`BUY 10 ${symbol.toUpperCase()}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load signal.");
    } finally {
      setLoading(null);
    }
  };

  const handleBacktest = async () => {
    setLoading("backtest");
    setError(null);

    try {
      const nextBacktest = await runBacktest(symbol, interval);
      setBacktest(nextBacktest);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not run backtest.");
    } finally {
      setLoading(null);
    }
  };

  const handleExecute = async () => {
    setLoading("execute");
    setError(null);

    const payload: ExecuteTradePayload = {
      commandText,
      dryRun,
    };

    try {
      const result = await postExecutionCommand(payload);
      setExecutionResult(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not execute order.");
    } finally {
      setLoading(null);
    }
  };

  const pricePath = createPath(signal?.bars.map((bar) => bar.close) ?? []);
  const equityPath = createPath(backtest?.equityCurve.map((point) => point.equity) ?? []);

  return (
    <main className="shell">
      <section className="hero">
        <div>
          <p className="eyebrow">Next.js + FastAPI</p>
          <h1>AlgoTrader</h1>
          <p className="lede">
            Stock-focused trading support workspace for indicator-based signals,
            lightweight backtesting, and broker-safe execution commands.
          </p>
        </div>

        <div className="hero-card">
          <p className="card-label">Recommended stack</p>
          <ul className="stack-list">
            <li>Frontend: Next.js App Router</li>
            <li>Backend: FastAPI</li>
            <li>Free data path: Alpha Vantage</li>
            <li>Execution path: Alpaca paper trading</li>
          </ul>
        </div>
      </section>

      <section className="panel controls">
        <div className="control-group">
          <label htmlFor="symbol">Symbol</label>
          <input
            id="symbol"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value.toUpperCase())}
            placeholder="AAPL"
          />
        </div>

        <div className="control-group">
          <label htmlFor="interval">Interval</label>
          <select
            id="interval"
            value={interval}
            onChange={(event) => setInterval(event.target.value as (typeof intervals)[number])}
          >
            {intervals.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        <button onClick={() => void handleAnalyze()} disabled={loading !== null}>
          {loading === "signal" ? "Scanning..." : "Refresh Signal"}
        </button>
        <button onClick={() => void handleBacktest()} disabled={loading !== null}>
          {loading === "backtest" ? "Testing..." : "Run Backtest"}
        </button>
      </section>

      {error ? <p className="error-banner">{error}</p> : null}

      <section className="grid">
        <article className="panel">
          <div className="panel-heading">
            <div>
              <p className="card-label">Live signal</p>
              <h2>{signal?.symbol ?? symbol}</h2>
            </div>
            <span className={`badge badge-${signal?.signal ?? "hold"}`}>
              {signal?.signal?.toUpperCase() ?? "WAIT"}
            </span>
          </div>

          <div className="metrics">
            <div>
              <span className="metric-label">Last price</span>
              <strong>{formatMoney(signal?.latestPrice)}</strong>
            </div>
            <div>
              <span className="metric-label">Confidence</span>
              <strong>{signal ? `${signal.confidence}%` : "N/A"}</strong>
            </div>
            <div>
              <span className="metric-label">Score</span>
              <strong>{signal?.score ?? "N/A"}</strong>
            </div>
            <div>
              <span className="metric-label">Data mode</span>
              <strong>{signal?.mode ?? config?.marketData.mode ?? "N/A"}</strong>
            </div>
          </div>

          <p className="summary">{signal?.summary ?? "Load a symbol to generate a signal."}</p>

          <div className="chart-card">
            <div className="chart-header">
              <span>Price action</span>
              <span>{signal?.latestBarTime ? new Date(signal.latestBarTime).toLocaleString() : ""}</span>
            </div>
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Price chart">
              <path d={pricePath} />
            </svg>
          </div>

          <div className="signal-columns">
            <div>
              <p className="card-label">Drivers</p>
              <ul className="component-list">
                {(signal?.components ?? []).map((component) => (
                  <li key={component.name}>
                    <strong>{component.name}</strong>
                    <span>{component.note}</span>
                    <code>{component.score}</code>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <p className="card-label">Risk map</p>
              <ul className="risk-list">
                <li>Long stop: {formatMoney(signal?.risk.longStopLoss)}</li>
                <li>Long target: {formatMoney(signal?.risk.longTakeProfit)}</li>
                <li>Short stop: {formatMoney(signal?.risk.shortStopLoss)}</li>
                <li>Short target: {formatMoney(signal?.risk.shortTakeProfit)}</li>
              </ul>
            </div>
          </div>
        </article>

        <article className="panel">
          <div className="panel-heading">
            <div>
              <p className="card-label">Backtest</p>
              <h2>Strategy check</h2>
            </div>
          </div>

          <div className="metrics">
            <div>
              <span className="metric-label">Strategy return</span>
              <strong>{formatPercent(backtest?.totalReturnPct)}</strong>
            </div>
            <div>
              <span className="metric-label">Buy and hold</span>
              <strong>{formatPercent(backtest?.buyHoldReturnPct)}</strong>
            </div>
            <div>
              <span className="metric-label">Max drawdown</span>
              <strong>{formatPercent(backtest?.maxDrawdownPct)}</strong>
            </div>
            <div>
              <span className="metric-label">Sharpe</span>
              <strong>{backtest?.sharpeRatio?.toFixed(2) ?? "N/A"}</strong>
            </div>
          </div>

          <div className="chart-card">
            <div className="chart-header">
              <span>Equity curve</span>
              <span>{backtest ? `${backtest.trades.length} trades` : ""}</span>
            </div>
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Equity chart">
              <path d={equityPath} />
            </svg>
          </div>

          <div className="table-shell">
            <table>
              <thead>
                <tr>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>Qty</th>
                  <th>P/L</th>
                  <th>Return</th>
                </tr>
              </thead>
              <tbody>
                {(backtest?.trades ?? []).slice(-6).map((trade, index) => (
                  <tr key={`${trade.entryTime}-${index}`}>
                    <td>{new Date(trade.entryTime).toLocaleDateString()}</td>
                    <td>{new Date(trade.exitTime).toLocaleDateString()}</td>
                    <td>{trade.quantity}</td>
                    <td>{formatMoney(trade.profitLoss)}</td>
                    <td>{formatPercent(trade.returnPct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </article>
      </section>

      <section className="grid bottom-grid">
        <article className="panel">
          <div className="panel-heading">
            <div>
              <p className="card-label">Execution</p>
              <h2>Command gate</h2>
            </div>
            <span className={`pill ${config?.execution.liveEnabled ? "pill-live" : "pill-paper"}`}>
              {config?.execution.liveEnabled ? "Live armed" : "Paper / simulated"}
            </span>
          </div>

          <label htmlFor="commandText">Order command</label>
          <input
            id="commandText"
            value={commandText}
            onChange={(event) => setCommandText(event.target.value.toUpperCase())}
            placeholder="BUY 10 AAPL"
          />

          <label className="checkbox">
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(event) => setDryRun(event.target.checked)}
            />
            Simulate only
          </label>

          <button onClick={() => void handleExecute()} disabled={loading !== null}>
            {loading === "execute" ? "Submitting..." : "Submit Command"}
          </button>

          <p className="summary">
            {executionResult?.message ?? config?.execution.note ?? "Execution status will appear here."}
          </p>
        </article>

        <article className="panel">
          <div className="panel-heading">
            <div>
              <p className="card-label">Project map</p>
              <h2>Starting choices</h2>
            </div>
          </div>

          <ul className="recommendation-list">
            {(config?.recommendations ?? []).map((item) => (
              <li key={item.area}>
                <strong>{item.area}</strong>
                <span>{item.choice}</span>
                <p>{item.reason}</p>
              </li>
            ))}
          </ul>
        </article>
      </section>
    </main>
  );
}
