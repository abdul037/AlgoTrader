export interface PriceBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SignalComponent {
  name: string;
  stance: "bullish" | "bearish" | "neutral";
  weight: number;
  score: number;
  value: number | string | null;
  note: string;
}

export interface SignalRiskLevels {
  longStopLoss: number | null;
  longTakeProfit: number | null;
  shortStopLoss: number | null;
  shortTakeProfit: number | null;
}

export interface SignalReport {
  symbol: string;
  interval: string;
  provider: string;
  mode: "live" | "delayed" | "demo";
  generatedAt: string;
  latestPrice: number;
  latestBarTime: string;
  score: number;
  confidence: number;
  signal: "buy" | "sell" | "hold";
  summary: string;
  components: SignalComponent[];
  risk: SignalRiskLevels;
  bars: PriceBar[];
}

export interface BacktestTrade {
  entryTime: string;
  exitTime: string;
  entryPrice: number;
  exitPrice: number;
  quantity: number;
  profitLoss: number;
  returnPct: number;
  barsHeld: number;
}

export interface BacktestResult {
  symbol: string;
  interval: string;
  startingCapital: number;
  endingCapital: number;
  totalReturnPct: number;
  buyHoldReturnPct: number;
  maxDrawdownPct: number;
  winRatePct: number;
  sharpeRatio: number;
  trades: BacktestTrade[];
  equityCurve: Array<{ time: string; equity: number }>;
  generatedAt: string;
}

export interface ProviderStatus {
  provider: string;
  ready: boolean;
  mode: "live" | "delayed" | "demo";
  note: string;
}

export interface TradingConfig {
  appName: string;
  marketData: ProviderStatus;
  execution: {
    provider: string;
    ready: boolean;
    liveEnabled: boolean;
    paper: boolean;
    note: string;
  };
  recommendations: Array<{
    area: string;
    choice: string;
    reason: string;
  }>;
}

export interface ExecuteTradePayload {
  commandText: string;
  dryRun: boolean;
}

export interface ExecutionResult {
  accepted: boolean;
  simulated: boolean;
  broker: string;
  message: string;
  submittedAt: string;
  order: {
    symbol: string;
    side: "buy" | "sell";
    quantity: number;
    orderType: "market";
    timeInForce: "day";
    externalId?: string;
    status: string;
  };
}
