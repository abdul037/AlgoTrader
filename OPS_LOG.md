# AlgoTrader Ops Log

Append-only record of every operational action taken on the paper-trading bot:
deploys, config changes, incidents, decisions, and what was verified. Newest
day first, entries within a day in chronological order (UTC). Long-form
post-mortems and roadmap live in `ROADMAP.md`; this file is the action ledger.

Standing constraints: PAPER ONLY (`ENABLE_REAL_TRADING=false`,
`EXECUTION_MODE=paper`). Hard risk gates (spread cap, reward:risk, valid
bracket, relative-volume floor, regular hours, blacklist) are never loosened
without explicit operator sign-off recorded here.

---

## 2026-10-06 (Tue) — real-money code review fixes

- Mon 10-05 session (eToro LIVE, AlgoBot): two mirrored 1x entries, MSFT $999 at
  17:02 UTC (stop 511.02, target 544.52, momentum_breakout) and META $998.90 at
  19:15 UTC (stop 661.90, target 928.04, etf_mega_cap_relative_strength_rotation).
  Both have bot backup stops recorded. ETH test still open. One watch read timed
  out at 23:47 UTC and the next tick recovered. Not halted; equity basis $9,988.
- Independent review (3 reviewers) of the real-money paths. Verified and fixed:
  - Mirror writes its state (daily count, open symbol, 2x flag, backup stop)
    *before* the POST. It rolls back on a 4xx refusal or rejected/cancelled
    status, and a rate limit never halts it. A rejected order no longer counts
    as a successful 1x.
  - The evidence gate uses the order's own timeframe (scanner proposals have no
    signal). Missing timeframe → blocked. Crypto (incl. bare "BTC"/"ETH") is
    never mirrored by the stock mirror. The mirror is skipped when the paper
    order FAILED/BLOCKED.
  - Backup stop and ETH watch ignore malformed/empty portfolio reads (no stop
    dropped, no false "closed"). A stop is dropped only after the position was
    seen and is now gone. A rate-limited close keeps the stop and doesn't halt.
  - eToro rate-limit cooldown is per account (a demo 429 no longer blinds the
    live backup stop).
  - Reconciliation: one sweep at a time. Alpaca 40410000 "position not found" is
    treated as already flat. Stream updates invalidate the write-skip cache.
  - Readiness counts only if computed within 2 h. Evidence older than 24 h is
    blocked. Bad trade rows are skipped. The phase-gate refreshes run
    independently.
  - **2x leverage test held** (`LEVERAGE_TEST_ENABLED = False`): eToro's Amount is
    margin, so it would be $2,000 notional. Awaiting the operator's decision.
- Tests: 833 passed before the hold flag; eToro/phase suites 76 passed after.

## 2026-10-05 (Mon)

- **05:09 UTC** `BACKTEST_SCHEDULER_INTERVAL_SECONDS` restored 300 → **1800**. The trigger was due Sun 22:00 UTC but ran late
  because the session was idle. Deploy `a3c390ea` **SUCCESS 05:13 UTC**. Phase 1 was already complete and reported on 10-04.
- **07:36 UTC** the eToro guard heartbeat resumed after the restart (last 07:35:02). There have been 0 guard errors since 10-04
  20:00, and the mirror is not halted. ETH test still open: **$2,722.99** vs entry $2,698.16, so **+$9.20 before fees** (≈ −$0.80
  after the $10 buy fee).
- **Operator: "fix the timeout now."** Diagnosed from `run_logs`: one maintenance run took ≈ 280 s against a 240 s limit
  (timed out 174 times in 24 h). Hot spots: **Alpaca reconciliation ≈ 140 s** and the **open-signal check ≈ 100 s** (73
  signals). Cause: the sweep re-reads all orders (≤ 500 plus legs) every minute and rewrote every order row, plus an
  execution update and a learning event per trade, unconditionally. Fix: write only when the broker-state fingerprint
  changed (cleared every 15 min and on restart for a full rewrite; recorded only after a successful write). The
  open-signal check now fetches one quote per symbol/timeframe and skips the DB write while a price is unchanged.
  Safety logic is unchanged (protection, unknown-position and breaker checks still read every order). 817 tests pass.
- **09:05 UTC verified:** deploy `4437d4d4` SUCCESS 08:35 UTC. **0 timeouts since** (14 in the 2 h before). Alpaca
  reconciliation **≈ 9-10 s** (was ≈ 140 s); the 15-minute full rewrite still takes ≈ 142 s, so that run is ≈ 170 s, under
  the limit. Open-signal check **≈ 7 s** (was ≈ 100 s). Maintenance now completes every 1-2 min instead of timing out.
  eToro guard heartbeat fresh (09:05:35); ETH test open at $2,719.88.

## 2026-10-04 (Sun) — Phase 1 complete (25/25 symbols, 5m + 15m + 1d)

- **Pooled walk-forward OOS verdicts:** 9 strategies pass, **all daily (1d)**: momentum_breakout
  (+$5.99/trade, 279 trades), ma_crossover (+$4.41), ema_trend_stack (+$3.28),
  relative_strength_momentum (+$2.65), atr_donchian_trend_breakout (+$2.28), trend_following (+$1.89),
  pullback_trend (+$1.64), etf_mega_cap_relative_strength_rotation (+$1.64),
  regime_aligned_trend_continuation (+$0.78); each over 31 symbols, holdout positive (14–25 trades).
- **Every intraday (5m/15m) strategy fails**: expectancy −$5 to −$37/trade over 100–1,400 OOS trades,
  holdout negative too. Best: rsi_reversal 15m +$0.37 but holdout −$0.98.
- **Variants (pooled, latest per symbol/strategy):**
  - hold_overnight is **worse** than closing at the bell: 15m −$11.56 vs −$8.35/trade; 5m −$13.46 vs
    −$9.49. Keep the close-before-the-bell rule.
  - stop floors help but don't rescue the intraday strategies: 1.0× session ATR → 15m −$5.28,
    5m −$5.17/trade (vs −$8.35 / −$9.49); still negative.
- **eToro-cost re-pricing (operator request; no API key used).** Finding: every backtest already ran on
  the default `CostModel()` = eToro *CFD* retail model (10 bps spread + 0.015%/day financing, weekend ×3).
  eToro's published fees for real, unleveraged US stocks (2026): $2 commission per side, no spread
  markup, no overnight fee. Re-priced every 1d OOS trade (removed modeled costs; applied $4 round trip
  + 4 bps market spread/slippage; pessimistic case 12 bps): momentum_breakout +$6.98/trade,
  ma_crossover +$2.76, relative_strength_momentum +$2.60, atr_donchian +$2.32, ema_trend_stack +$2.25,
  trend_following +$2.12, etf_mega_cap_rotation +$0.72; regime_aligned_trend_continuation −$0.02 and
  pullback_trend −$0.43 turn negative. Edges are 0.21–0.38% of notional before the flat fee, so the
  **flat $4 round trip needs positions of ≈$1,050–1,900 to break even and ≈$2,100–3,800 to keep half
  the edge** — account size and per-trade risk decide whether eToro live is viable.
- **Operator: "switch on Phase 2 on Alpaca paper."** Verified first: production candidates are
  `LiveSignalSnapshot` with `timeframe` (default "1d") and registry strategy names matching the verdict
  keys, so the gate can't block everything by accident. `REQUIRE_STRATEGY_OOS_EVIDENCE=true` set
  11:36 UTC; Phase 3 clock starts on the first enforced refresh. Only the 9 passing 1d strategies may
  create proposals; all intraday strategies become shadow signals.
- Operator asked about **eToro live small trades incl. leverage**. Choices: automatic within caps, $50 per
  trade, 1x first then one 2x test. Writing the real-money path was blocked twice by the session's
  auto-mode safety classifier; work was stashed (not deployed) until the operator changed the session's
  permission mode, then completed.
- `297fc1d` **capped eToro LIVE test mirror — deployed INERT.** Mirrors each Alpaca paper entry (Phase 2
  strategies only) as a real eToro position: ≤$100 hard cap (default $50), ≤2 new/day, ≤3 open, $25 daily
  loss stop, 1x + exactly one 2x test, long-only with stop+target, halts itself on any eToro error,
  closes any live position found without a stop. 776 tests pass. **Operator must set in Railway:**
  `ETORO_LIVE_API_KEY`, `ETORO_LIVE_USER_KEY` (secrets), `ETORO_LIVE_ACKNOWLEDGEMENT` (exact phrase),
  `ETORO_LIVE_MIRROR_ENABLED=true`. `ENABLE_REAL_TRADING` stays false.
- **11:53 verified:** deploy `ecdf45d9` (code `297fc1d`) SUCCESS; `strategy_evidence_refreshed` shows
  `enforced: true`; **Phase 3 clock started 2026-10-04T11:53:21Z** (`go_live_readiness`: 0/50 trades,
  0/4 clean weeks). First live Phase 2 session: Mon 10-05 (check armed 15:30 UTC).
- **12:08** operator set the eToro live Railway variables (deploy `ca085bdb`). **12:23**
  `etoro_live_reconciled`: equity $10,000, no positions. Operator said the account should hold $500, so I
  suspected the client's simulation placeholder and pushed `2204ad9` (mirror refuses a simulated client and
  logs `etoro_live_client_unusable` with the base-URL host). It deployed as `baa22bb3` (SUCCESS 12:32).
- **12:39 verified, live keys WORK.** The guard did not fire, and Railway logs show real
  `GET https://public-api.etoro.com/api/v1/trading/info/portfolio` calls returning **200** at 12:39:32
  (`/pnl` 404 is expected and handled). So the AlgoBot account itself reports **$10,000 credit, 0
  positions**. The operator's screenshot explains the $500: it is a **copy** from their main account into
  AlgoBot ("Copy started 04/10/2026 15:32", invested $500). AlgoBot trades are copied in proportion to
  AlgoBot's $10,000, so a $50 mirror trade (0.5%) puts about **$2.50** of the operator's $500 to work. The
  $25 daily loss stop is measured on the $10,000 base. Sizing decision is put to the operator; caps are
  unchanged. The operator confirmed the keys are from AlgoBot, Real environment, with Write permission.
- **Operator: "yes, build the 10% sizing and deploy."** `515855a`: each mirror position is **10% of the eToro
  equity last read by reconcile** (was a fixed $50). With AlgoBot at $10,000 that is $1,000, about **$50 of the
  operator's $500 copy**. Hard caps in code: ≤10% of equity and ≤$1,000 per position; a 5% daily equity-drop
  stop (≈$25 on the copy; replaces the $25 fixed stop); no trade while equity is unknown or below eToro's $10
  minimum; 2 new/day, 3 open and one 2x test unchanged. Setting `ETORO_LIVE_TRADE_AMOUNT_USD` replaced by
  `ETORO_LIVE_TRADE_PCT_OF_EQUITY` (default 10, capped at 10). 779 tests pass. Deploy `cb22b99b` **SUCCESS
  12:54 UTC**; stored equity basis is $10,000. Operator advised to **Keep Copying**: stopping the copy doesn't
  stop the bot, it only stops the $500 following it. First possible mirrored trade: Mon 10-05 13:30 UTC.
- **Operator: skip the Notion mirror for now** (the repo log stays the source of truth).
- **Operator asked for a weekend eToro live test on crypto.** Correction given: crypto is OFF on the main
  bot since 09-27, so there were no ETH/BTC trades to copy, and the eToro client only resolved US equities.
  Operator choices: **ETH, $200 in AlgoBot (≈$10 of the $500 copy), close "based on the strategy and profit
  made"**. Built `app/broker/etoro_live_test_order.py`: a one-shot request row
  (`etoro_live:test_order_request`) makes the bot place one 1x ETH buy with stop = entry − 1.5× daily ATR(14)
  (clamped 1–10%) and target = 2R, both held by eToro, plus a 7-day time stop. The same live-mirror locks apply
  (enabled, acknowledgement, not halted, real client). Hard cap $200, ETH/BTC only, one test at a time. A
  failed test is logged and never halts the mirror. Positions are now closed by position id
  (`close_position_by_id`), so the mirror's unprotected-position close also works for non-equity
  instruments. eToro's crypto fee is about 1% a side. 790 tests pass.
- **Operator: "Increase the copy value from 10 to 50$."** Before any order was sent (the $200 request was never
  written; its scheduled trigger was cancelled), the test cap was raised to **$1,000 in AlgoBot (≈$50 of the copy)**,
  also capped at 10% of the AlgoBot balance, the same per-trade limits as the live mirror. 32 related tests pass.
- **13:32 UTC** request written ($1,000 version live 13:29:33, old $200 container removed 13:29:52).
  **13:41:19 UTC (5:41 pm Dubai) ETH test SUBMITTED**: eToro order `1602717961`, $1,000 at 1x, entry ref
  **$2,698.02**, daily ATR $77.48 → **stop $2,581.80 (−4.31%)**, **target $2,930.47 (+8.62%)**, time stop
  Sun 10-11 13:41 UTC. The hook runs inside the demote step, so it fires on the maintenance tick that reaches it.
- **Found: eToro `credit` is cash only.** Right after the buy it read **$8,990** ($10,000 − $1,000 − ≈$10 fee,
  consistent with eToro's ~1% crypto fee). The mirror used it as equity, which would have shrunk later trades and
  tripped the 5% daily stop on money merely invested. Fixed: AlgoBot balance = credit + Σ position `amount`
  (at cost; open P&L counts on close, since the portfolio has no live value). The test's opening balance is now
  taken before the buy so its P&L is net of both fees. 792 tests pass.
- **Pages refreshed (operator: "update the artifacts … what's done and what's pending").** A 10-agent workflow
  gathered the ledger and live figures, redrafted the four pages and fact-checked each one. I then added the ETH test
  and the balance fix, and checked each page at 400 px and 1100 px in light and dark. That check caught a Console
  script error (`const top` clashes with `window.top`), which I fixed. Published to the same links: Ops (v2), Profit
  Roadmap (v18), Console (v2), Architecture (v3).
- **13:49:43 UTC ETH test FILLED** (first seen by the bot): position `3595777383`, **0.370622 ETH @ $2,698.16**
  ($1,000). eToro kept the target **$2,930.47** but set the stop-loss to **$2,428.34 = exactly −10%** of the
  fill, not the bot's $2,581.80 (−4.31%). eToro appears to have overridden the requested stop (cause not
  confirmed). Worst case at eToro's stop: ≈ −$100 + fees in AlgoBot (≈ −$6 on the copy) vs the planned ≈ −$63
  (≈ −$3). Monday's mirrored stock trades may be affected the same way; I'm putting a software stop to the operator.
- Balance fix `37327a5` deploy `45f40753` **SUCCESS 13:52:55 UTC**. Corrected the open test's opening balance to
  $10,000 (pre-buy) so its P&L is net of both fees, and forced a phase-gate run so the mirror re-reads the AlgoBot
  balance as cash + invested (≈ $9,990; the 13:49 reading of $8,990 came from the old container).
- **Operator: "Add a backup stop in the bot."** `app/broker/etoro_live_backup_stop.py`: on every maintenance tick the
  bot reads a fresh eToro price and closes a live position itself once it is at or below the strategy's stop. This
  applies to the ETH test now (stop $2,581.80; eToro's own −10% stop at $2,428.34 stays as the outer net) and to
  every mirrored stock entry from Monday (its intended stop is recorded at entry). There is no price, so no close: eToro's stop
  still holds. A failed close halts new mirror entries and asks for a manual close. Ticks are minutes apart, so a fast
  move can fill below the level. 799 tests pass.
- **14:49 verified:** backup-stop deploy `3b5af992` SUCCESS 14:17:39 UTC. The ETH monitor reads fresh eToro prices
  (last $2,692.37 at 14:48 UTC, open −$2.15 before fees). The mirror balance now reads **$9,990** (cash + invested), so
  no false daily loss stop.
- **Operator: "build the separate backup-stop job".** `app/broker/etoro_live_guard.py`: a daemon thread runs the
  ETH-test watch and the backup stops **every 60 s**, independent of the sequential scheduler. Before, they ran inside
  maintenance, which hits its 240 s limit about 160 times a day and waits behind ~3-minute backtest passes. With nothing
  open the thread makes no eToro call. It writes a heartbeat, and maintenance runs the same checks only when that
  heartbeat is older than 5 min. One lock serializes every live-eToro state change in the process.
- **Independent review before deploy** (15-agent workflow: 3 lenses, each finding verified): 6 confirmed (1 medium,
  5 low), 5 rejected. All fixed. (1) After a failed close, an empty or malformed portfolio read now counts as *still
  open* (fail closed), and a re-read follows 3 s later. (2) The test-order and backup-stop steps are isolated, so
  one failing can't skip the other. (3) Saves merge instead of overwriting, and a test another container already
  finished is never overwritten or re-announced. The new container's guard also waits 30 s, past the ~20 s deploy
  overlap. (4) Shutdown stops the guard first and joins it (5 s). The test order is saved before it is sent. A bad
  amount is recorded as failed. An unclear order result keeps being watched for 30 min instead of being marked failed.
  813 tests pass.
- **Verified live:** guard deploy `56d1c5f0` SUCCESS 17:00:19 UTC. At 20:01 UTC the guard heartbeat and the ETH watch
  both updated within the last minute (heartbeat 20:01:01, price read 20:01:00). There were 0 `etoro_live_guard_error`
  events since the deploy, and the mirror is not halted. ETH $2,700.16 vs entry $2,698.16 (+$0.74 before fees). The
  balance reads $9,990.
- Recommendation to operator: (a) turn on REQUIRE_STRATEGY_OOS_EVIDENCE (trade the 9 daily strategies
  only; intraday become shadow signals); (b) keep the intraday close rule; (c) no live stop floor
  (intraday won't trade anyway). (a) signed off and enforcing since 11:36 UTC (see above); (b) and (c)
  stand as recommended.

## 2026-10-03 (Sat) — loss analysis and three follow-ups (operator: "do all three")

- **Why the losses (all-time realized -$369.82, 18 trades):** 4 wins +$984 (avg $246) vs 14
  losses -$1,354 (avg $97). Win/loss size 2.5x → break-even win rate ~29%; actual 22%.
  09-28 alone (4 tech longs, one down day) = 44% of losses; NFLX 09-14 = 25%. Overnight holds
  netted **+$784** (INTC, IWM, CSCO, COST) — the new close-before-the-bell rule would have cut
  them; flagged as an open question, now measured by the hold_overnight backtest variant.
- **Correction:** stops were *not* inside bar noise — they were 1.6–4.4x the 5m/15m bar ATR
  (NVDA 3.5x, AAPL 4.4x, META 1.6x). The mismatch is vs. a multi-hour hold / the day's range.
  So the stop floor ships as a measured backtest variant, live floor OFF until evidence.
- `c1c913c` **daily cap of 2 entries per correlation bucket** (`MAX_DAILY_ENTRIES_PER_CORRELATION_BUCKET=2`);
  QQQ/CSCO → tech_complex, SPY/IWM/DIA → broad_market (also tightens the correlated-exposure cap).
- `0210f0b` **intraday walk-forward**: 5m/15m fold plan (~120 days, 7-day folds, 14-day holdout),
  scheduler timeframes 1d,15m,5m; backtest closes intraday trades at the session end (mirrors
  live); variants hold_overnight / stop_floor_0.5 / stop_floor_1.0 logged to run_logs only
  (never gate rows). Engine 18x faster (5m run 158 s → 8.7 s, identical trades) via a causal
  indicator prefix cache; opening range made causal (it filled bars 1–4 from bars 2–5 when
  computed over a whole frame). Backtest cursor can resume mid-symbol. 745 tests pass.
- Pending: confirm prod produces 5m/15m OOS rows (check armed 19:08 UTC); per-strategy
  intraday report once rows accumulate.
- **18:30 verified:** intraday walk-forward producing in prod — 16 × 15m + 2 × 5m OOS rows and
  54 variant results in the first hour; mid-symbol resume working (start_unit 26 → 38). Full
  universe ≈ 30 h at a 180 s pass every 30 min → table by Mon evening.
- **Plan agreed (operator):** Phase 1 evidence (now) → Phase 2 trade only passing strategies +
  market-direction filter → Phase 3 prove in paper (50+ trades, total wins ≥ 1.3× total losses,
  max drawdown < 3%, 4 clean weeks) → Phase 4 micro-live only on operator decision.
- `e3c34b5` **market-direction filter** (operator: "yes, build it"): new intraday buys blocked
  while SPY (and QQQ for tech) is below today's VWAP and ≥0.3% below the prior close
  (`MARKET_DIRECTION_MIN_DROP_PCT=0.3`). Swing entries exempt; missing data never blocks (logged).
  753 tests pass. Live verification armed Mon 10-05 14:20 UTC (data reachability for SPY/QQQ 5m).
- **19:02 Operator: "speed it up for the weekend."** `BACKTEST_SCHEDULER_INTERVAL_SECONDS`
  1800 → **300** (weekend only, market closed). The variable change did not trigger a deploy
  (again); manual redeploy of `b6def479` (code `e3c34b5`) → `a4d6ebb9`. **Restore to 1800**
  armed for Sun 10-04 22:00 UTC, before Monday's open, with a Phase 1 completion check.
- **Operator: "build Phase 2, 3 and 4."** `6f9378a` — built, not switched on:
  - Phase 2: pooled per-strategy OOS evidence verdicts (≥40 OOS trades, positive expectancy
    after costs, ≥10 holdout trades with positive holdout expectancy). Gate
    `REQUIRE_STRATEGY_OOS_EVIDENCE` **OFF** pending the operator's decision on the Phase 1 table.
  - Phase 3: go-live readiness tracker (≥50 trades, PF ≥1.3, max DD <3%, 4 clean weeks) + R
    scorecard, refreshed every 30 min; `GET /performance/go-live-readiness`. **Auto-demotion now
    enforced** (`STRATEGY_AUTO_DEMOTE_ENABLED` default true; needs 20+ live trades per strategy).
  - Phase 4: live mode additionally locked behind readiness met + micro-live cap ≤0.1% + an exact
    operator acknowledgement phrase. `ENABLE_REAL_TRADING` untouched (false).

## 2026-10-02 (Fri) — session results (checked 10-03)

- Deploy `0ac8b99` SUCCESS 10:42 UTC (intraday close-before-bell live for the session).
- **IWM** (15m) hit its target at the open, 282.45 → **+$193.15**. **CSCO** (5m) hit its target
  at the open, 110.18 → **+$168.72**. Both were the overnight carries from 10-01 and gapped
  in our favour; they closed on their brackets before the flatten window.
- **AMZN** (15m, anchored_vwap_pullback_continuation) entered 13:42 @ 252.70 ×49; **closed by
  the new intraday flatten at 19:51 UTC** (8.6 min to close) @ 251.61 → **-$53.41**. First
  live run of `intraday_exit`: legs cancelled, market close filled, reconciliation booked the
  loss via `exit_fill` (so the loss gates saw it) — verified.
- **TSLA** (1d, ema_trend_stack) entered 17:00 @ 372.29 ×18 — swing, holds over the weekend
  with its GTC bracket. **NVDA** (1d) still open with its bracket.
- No `reward_to_risk_below_min_at_quote` or re-entry-cooldown blocks fired. 30 proposals
  blocked, nearly all by the 30% gross-exposure cap.
- **Friday realized: +$308.46.** All-time realized: **-$369.82** over 18 closed trades
  (4 wins / 14 losses). Open: NVDA +$78.54, TSLA -$30.60. Equity **$99,743.01** (10-03).

## 2026-10-02 (Fri) — Review Team meeting (first since 09-07)

Convened locally over the 31 unreviewed commits (`0a12efe..2575a1b`) plus the week's trades.
- 🧪 **QA — BLOCK (crypto); equity fixes OK.** Crypto breaker trip on the first fill
  (`BTCUSD` position vs `BTC/USD` order) and crypto/flatten losses invisible to the loss gates.
  Acted: crypto OFF 09-27; `7ad434f` sizing headroom; `2575a1b` flatten P&L + live-stop-only
  protection. Open nits: heat estimate assumes 1%/position; dead `consecutive_losses()`.
- 📈 **Financial Strategy — LIKELY HARMS EDGE.** 9 of 10 executions came from intraday
  strategies (vwap_reclaim 5m, intraday_vwap_trend 15m, rsi_reversal 15m,
  anchored_vwap_pullback_continuation 5m, relative_volume_reclaim_continuation 5m) with **no
  walk-forward OOS evidence at all** — the walk-forward runs only on 1d. Exploration mode skips
  the backtest gate (`PAPER_EXPLORATION_REQUIRE_BACKTEST_VALIDATED=false`). Best measured 1d
  OOS edge (`regime_filtered_mean_reversion`, +$31/trade over 465) is not being traded; several
  approved 1d strategies lose OOS. `strategy_not_production_approved` actually means "no
  exploration approval row". Backtester diffs judged sound.
- 💹 **Trader — RISK CONCERNS.** Slippage fine (≤20 bps, stop slip ≤0.08R). Findings:
  no reward:risk re-check at the fill price (AAPL 1.20 at proposal → 0.69 at fill; drift gate
  is a flat 35 bps, not relative to the stop); no same-symbol re-entry cooldown after a stop
  (NVDA re-bought 1h43m later); intraday strategies held overnight with intraday stops
  (INTC 2 days, COST overnight); stops 0.3–1.5% are inside 5m noise (6/7 stopped); the $12.5k
  notional cap, not the 0.5% budget, sets risk ($40–$248/trade) — judge strategies in R.
  Keep the 30% gross cap. Its top finding (loss gates blind on 09-28) was **checked and
  rejected**: limit is 4 losses, only 2 had closed at 16:29.
- 📋 **PM — HOLD on scaling; keep paper running.** Tightening fixes (no sign-off needed):
  R:R re-check at the live quote, same-symbol re-entry cooldown, flatten intraday trades
  before the close. Needs operator decision: require OOS evidence before a strategy
  auto-trades (would pause all intraday strategies until intraday walk-forward exists).
  Keep 30% gross cap and crypto OFF.
- **Operator decision 10-02: "Keep exploring for now."** Intraday strategies keep trading on
  paper without OOS evidence; judged strictly on live results; intraday walk-forward to be
  built, then `PAPER_EXPLORATION_REQUIRE_BACKTEST_VALIDATED` turned on once it exists.
- **Shipped 10-02** (726 tests pass):
  - `b78db7f` (live 07:59 UTC) — reward:risk re-checked at the live quote before submit
    (`EXECUTION_MIN_REWARD_TO_RISK_AT_QUOTE=1.0`); re-entry cooldown after a losing exit
    (`REENTRY_COOLDOWN_MINUTES_AFTER_LOSS=240`); fixed a calendar-expired test fixture.
    Also: the portfolio-heat *estimate* now assumes the enforced 0.5% per open position instead
    of 1%. That admits more positions under the unchanged 6% heat cap (it is not purely a
    tightening, contrary to the commit message); no practical effect while the 30% gross cap
    holds the book at ~3 positions.
  - `0ac8b99` — intraday-timeframe positions closed in the last 10 min of the session
    (`INTRADAY_FLATTEN_MINUTES_BEFORE_CLOSE=10`, Alpaca clock); 1d+ swing positions keep GTC
    brackets. First live run: today's close (IWM 15m and CSCO 5m are open from 10-01).

## 2026-09-28 (Mon) → 2026-10-02 (Fri pre-open)

- **Shipped before the 09-28 open:** `7ad434f` (size 2% under the enforced 0.5% cap, deploy
  SUCCESS 12:34 UTC) and `2575a1b` (book flatten-close P&L for the loss gates; bracket counts as
  protected only with a live stop leg). Crypto trading switched OFF 09-27 16:43
  (`CRYPTO_TRADING_ENABLED=false`) after the QA Bot found it can trip the breaker on the first
  fill (Alpaca `BTCUSD` position vs `BTC/USD` order) and its stop losses bypass the loss gates.
- **Trading resumed — 10 executions 09-28..10-01**, all with Alpaca brackets; every exit was a
  bracket leg (no flattens):

  | Day | Symbol | Qty | Entry | Exit | P&L |
  |---|---|---|---|---|---|
  | 09-28 | NVDA | 53 | 232.36 | stop 229.23 | -165.66 |
  | 09-28 | AAPL | 36 | 342.53 | stop 340.17 | -84.98 |
  | 09-28 | NVDA | 53 | 232.01 | stop 228.47 | -187.35 |
  | 09-28 | META | 17 | 724.91 | stop 715.33 | -162.86 |
  | 09-29 | INTC | 107 | 116.30 | target 121.40 | **+545.71** |
  | 09-29 | QQQ | 16 | 739.19 | stop 736.70 | -39.84 |
  | 09-29 | COST | 13 | 922.67 | stop 913.10 | -124.42 |
  | 10-01 | IWM | 44 | 278.06 | open | +82.72 unrl |
  | 10-01 | CSCO | 114 | 108.70 | open | +28.73 unrl |
  | 10-01 | NVDA | 22 | 230.38 | open (swing, stop 208.94) | +31.90 unrl |

  Closed: 7 trades, 1 win / 6 losses, **net -$219.40**. Open +$143.35 unrealized. Equity
  **$99,531.02** (10-02 05:58 reconciliation, 3 positions, 0 issues) vs $99,607.60 on 09-27.
  Largest loss $187 (0.19% of equity), inside the ~$490 per-trade budget; stops held.
- **09-28 (corrected 10-02):** the 4th loss closed at 19:50 UTC, ten minutes before the bell, so
  the per-day cooldown (4 losses) never had to block anything. The 16:29 NVDA/META entries were
  correctly allowed: 2 losses (-$250) at that point, well inside the 4-loss / $3,000 limits. An
  earlier version of this line claimed the cooldown halted entries — that was unverified.
- **Remaining blocker — portfolio gross-exposure cap.** 74 `auto_proposal_failed` since 09-28,
  nearly all "Projected gross exposure exceeds the portfolio limit"
  (`PORTFOLIO_MAX_GROSS_EXPOSURE_PCT` default 30%). At ~$12.4k per position, 3 positions fill
  it, so `MAX_OPEN_POSITIONS=8` is unreachable. This is a portfolio risk gate: **not changed;
  needs operator sign-off.**
- **Flagged:** AAPL 09-28 filled at 342.53 with target 344.16 / stop 340.17 — realized
  reward:risk 0.69 after entry drift (planned R:R passed the gate at the proposed price).
  The entry-drift check let it through; to investigate.
- `strategy_not_production_approved` still blocks some swing proposals (9 since 09-28);
  `workflow_cadence` 240s timeouts persist (236 since 09-28).

## 2026-09-27 (Sun) — covers 2026-09-14 → 09-27

- **Account state:** flat, equity $99,607.60. Last trade 09-15; the 5 trades of the
  aggressive run net **-$447.38** (NFLX price feed verified correct — ~10:1 split).
- **Why stocks stopped trading after 09-15 (diagnosed 09-27).** 170 `auto_proposal_failed`
  + 10 `auto_proposal_safety_blocked` since 09-16:
  - 133× "Trading halted after 4 consecutive losses today" — **bug**: the cooldown read
    the *all-history* loss streak. Only a win resets it and a halted bot cannot trade, so
    the 09-15 streak was a permanent lockout.
  - 37× "Estimated trade risk 0.53–1.00% exceeds the 0.50% cap" — sizers sized to
    `MAX_RISK_PER_TRADE_PCT` (1.0%) while `INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED`
    tightens the gate to `portfolio_future_max_risk_per_trade_pct` (0.5%).
  - 10× `strategy_not_production_approved` (AMZN/MSFT/NVDA, swing_scan) — open, to investigate.
  - Scan-level rejections are dominated by `relative_volume_too_low` (quiet market; floor unchanged).
- **Operator sign-off 09-27:** (1) loss-streak cooldown resets each trading day (still
  halts for the rest of the day at 4); (2) option (a) — size trades to the gate's
  effective cap (0.5% paper, 0.1% live) rather than raising the cap. Rationale: keep
  per-trade losses small ahead of any future real-money stage.
- **Fix `40b6f05`** (713 tests pass): shared `effective_max_risk_per_trade_pct()` used by
  the auto-proposal sizer and `TraderService`; `build_risk_context` uses today's streak.
  Hard gates unchanged.
- **Also shipped this period:** `a920bab`/`22b05ad` crypto on Alpaca paper, 24/7 bucket
  (operator sign-off; now parked — "stocks first"); `4133195` rotating scan cursor so the
  whole universe is covered; `b63b8cf` backtest/scan-decision indexes (lookup 6070ms→6ms,
  recent read ~11.8s→102ms; built CONCURRENTLY in prod first); `acee004` stop persisting
  per-fold backtest rows (~37/run, table had 1.45M rows) and read the expectancy baseline
  from the OOS aggregate.
- **09:14 Deployed and verified.** Deployment `af731500` SUCCESS; boot preflight 31/31 symbols
  open, **0 global blockers**; paper-only policy confirmed; reconciliation clean (0 positions).
  `workflow_cadence` 240s timeouts down from 206/day (09-25) to 2 since boot after the 09-26
  indexes — watching. Notion mirror updated.
- **Next:** verify Mon 09-28 US session — proposals pass risk validation, scans evaluate
  more symbols per run, trades execute with brackets.

## 2026-09-13 (Sun)

- **Incident review — why nothing traded Thu/Fri.** The boot-time funnel preflight
  (shipped Thu, first run 13:16:57 UTC) reported all 25 universe symbols open but two
  global blockers: `automation_kill_switch_enabled` and `automation_paused`. Trace:
  Thu **13:06:20** (pre-market) reconciliation saw GOOGL with no live bracket legs and
  tried to flatten it while the *previous boot's* flatten (12:56:37) was still
  `pending_new`; Alpaca rejected the duplicate (`40310000 insufficient qty available…
  held_for_orders=1`). The rejection was recorded as `missing_bracket_protection:GOOGL`,
  the circuit breaker tripped, the kill switch + pause were persisted in `runtime_state`,
  and the emergency stop cancelled 1 order / closed 1 position. The 13:16 boot repeated
  the exact sequence. Nothing clears that state, so the scheduler logged
  `workflow_scheduler_paused` every minute from Thu 13:17 through Sun (≈4,300 events).
  Backtests kept running (they are not gated). **Thu/Fri: 0 proposals, 0 trades.**
- **Fix pushed `2efc6de`** (674 tests pass; 4 known sandbox-only failures): (1) a position
  with a live reducing order at the broker is "closing in flight", not unprotected — no
  re-flatten, no issue (`unprotected_position_closing_in_flight`); (2) a flatten rejected
  because the shares are already committed is deferred (`unprotected_position_flatten_deferred`),
  not a breaker — other flatten errors still raise the issue; (3) **paper-only self-healing**
  (`app/automation/auto_recover.py`): while the breaker is tripped the scheduler re-probes
  reconciliation every 10 min and, once clean, clears the breaker and resumes (max 3/day,
  `automation_auto_recover_probe` / `automation_auto_resumed`). It never overrides an
  operator pause, a manual kill switch, `KILL_SWITCH_ENABLED`, an account mismatch, a
  broker trading block, or real trading. Settings: `PAPER_AUTO_RECOVER_CIRCUIT_BREAKER`
  (on), `PAPER_AUTO_RECOVER_PROBE_INTERVAL_SECONDS=600`, `PAPER_AUTO_RECOVER_MAX_RESUMES_PER_DAY=3`.
  Safety note for the operator: the breaker itself is unchanged; only a *false* trip on a
  close already in flight is prevented, and recovery requires a clean reconciliation.
- **09:25–09:28 Deployed and verified.** Deployment `29b609e2` SUCCESS 09:27:50. Boot
  preflight still showed the two blockers (state persisted from Thursday); the first
  scheduler tick probed reconciliation at 09:27:39 (`orders_seen=31, positions_seen=0,
  issues=[]`), cleared the breaker and **auto-resumed at 09:28:29**
  (`automation_auto_resumed`, resume 1/3 today). Scheduler running again: maintenance,
  ledger cycle, open-signal check, backtests. GOOGL had been closed by Thursday's emergency
  stop at the open; the broker ledger booked it on resume: **GOOGL 1 sh, entry 338.75 →
  exit 328.94, realized −$9.81 (`broker_close`)**. Account flat, equity **$100,055.06**
  (−$9.85 vs the $100,064.91 baseline; the only closed trade to date).
- Next: Monday 2026-09-14 pre-open check armed for 13:20 UTC (health, resumed state,
  0 blockers in the boot preflight, proposals from the 13:30 open).

## 2026-09-12 (Sat) / 2026-09-11 (Fri)

- Paused all of Friday by the persisted kill switch (see 09-13). **0 proposals, 0 trades.**
  App healthy otherwise; the 30-min backtest refresh kept scoring the universe.

## 2026-09-10 (Thu)

- **12:17** Pre-open review of Wednesday (the session ran unattended; the scheduled
  check-ins fired but their reports were delivered late). Bot healthy the whole time:
  no reboot since Tue 20:17, events flowing, 0 QueuePool/SIGTERM. **Wednesday result:
  0 proposals, 0 trades.** Funnel: 59 near-miss + 85 weak-valid promotion attempts,
  15 candidates entered the proposal step, **15/15 failed** with `Instrument X is not
  supported in this version` (INTC ×8, AAPL ×3, META ×2, CSCO, MSFT); 2 swing candidates
  safety-blocked (`strategy_not_production_approved`, correct).
- **Root cause #1 — hidden second allowlist.** `InstrumentResolver` treats a hardcoded
  6-name catalogue (NVDA, GOOG, GOOGL, AMD, MU, GOLD) as exhaustive, so widening
  `ALLOWED_INSTRUMENTS` on Tue only moved the failure one check later. Fixed: catalogued
  names keep their metadata; any other allowlisted ticker resolves as a plain US equity.
- **Root cause #2 — GOOGL left unprotected overnight.** Brackets were submitted with
  `time_in_force=day`: at Tue's close the take-profit leg **expired** and the stop leg was
  **canceled**, so the 1-share GOOGL position sat all of Wednesday with no stop (it
  closed ~$330.65, below the $333.24 stop that no longer existed). Reconciliation did not
  flag it because it counted a bracket as protection if legs merely *existed*. Fixed:
  brackets are now GTC; reconciliation counts only live legs; in paper mode an owned
  position with no live protective leg is closed at market and logged
  (`unprotected_position_flattened`) instead of tripping the circuit breaker
  (`reconciliation_flatten_unprotected_positions`, default on, never with real trading).
- **12:52** Shipped all three fixes with tests (657 pass). Push-triggered deploy in
  progress; verification below. Expected on boot: GOOGL flattened by the first
  reconciliation, proposals flowing from the 13:30 open.
- **Overnight backtest coverage (post cash-fix):** all 25 universe symbols scored, 608
  out-of-sample summaries, 454 with trades; returns sane (annualized −11% … +6%, median
  ≈0). Per-strategy ranking (avg annualized, median PF, win rate): ema_trend_stack
  +0.5% / 1.21 / 51%; trend_following +0.3% / 1.11 / 49%; pullback_trend +0.1% / 1.06 /
  52%; momentum_breakout 0.0% / 0.97 / 46%; ma_crossover −0.2% / 1.05 / 50%; the
  mean-reversion / RSI families are negative on few trades. Holdout returns slightly
  negative for all. Honest read: the measured daily-bar edges are marginal; the plan's
  "concentrate on the top 3–4" now has a data basis (ema_trend_stack, trend_following,
  pullback_trend) and intraday timeframes are the next lever.
- **12:57** Armed midday (16:00 UTC) and post-close (20:10 UTC) checks.
- **13:06 / 13:16** Circuit breaker tripped twice on a false `missing_bracket_protection:GOOGL`
  (duplicate flatten while the first was pending) — automation paused for the rest of the
  week. Full trace and fix under 2026-09-13.
- **13:14–13:17** Shipped the boot-time funnel preflight (`207c6d9`, deployment `10b83c39`
  SUCCESS). First report: 25/25 symbols open, sizing cap $12,500, global blockers = the
  kill switch + pause above. It did its job: the blocker was visible in `run_logs` at boot.
- **Thursday result: 0 proposals, 0 trades** (paused from 13:06).

## 2026-09-09 (Wed)

- Unattended all day. Health: up since Tue 20:17, 0 errors other than the recurring
  `workflow_cadence` 240s timeouts (78 that day — still the throughput leak to fix).
  Backtest refresh ran every 30 min overnight and scored the full universe.
- **0 proposals / 0 trades** — root causes found and fixed Thu morning (see above).
  GOOGL held unprotected after its bracket legs died at Tue's close; equity ended the
  day ≈ $100,056 (−$8.7 vs baseline, all unrealized GOOGL).

## 2026-09-08 (Tue)

- **12:50** Verified the dashboard publish and found the bot had been DOWN since
  Mon 19:38 UTC: the Railway redeploy triggered by the `MARKET_UNIVERSE_SYMBOLS`
  variable change SIGTERM'd the running container and never started a
  replacement (`environment-status` showed no deployment). Restart policy
  `ALWAYS` did not help because nothing crashed; the replacement never came up.
- **12:51** Manual Railway `redeploy` → deployment `e32027c7` SUCCESS at 12:53.
  Startup policy log confirmed `execution_mode=paper`, `enable_real_trading=false`,
  near-miss auto-exec on. Alpaca paper account ACTIVE, equity $100,064.91, 0
  positions, reconciliation clean. Events flowing, 0 errors.
- **12:52** Scheduled routine `trig_01Dyf8QEda4fMDZeqCsGTztV`: pre-open health
  check + first-trade watch at 13:35 UTC.
- **12:54** Pushed `257fab9` (ROADMAP: Monday EOD, second silent stop, universe
  trim, dashboard). Push-triggered deployment `37ab2152` SUCCESS at 12:56:40 and
  replaced `e32027c7` cleanly. 2 clean boots today, 0 errors.
- **12:58** Operator asked for the auto-exec score floor to be lowered 65 → 55.
  **Not applied — it would be a no-op.** In the active paper-exploration profile
  the effective floor is `PAPER_EXPLORATION_AUTO_EXECUTION_MIN_SCORE`, which is
  set to `0.15` in Railway (almost certainly a typo for 15 or 55, but it means
  the score gate is already effectively open). `AUTO_EXECUTION_MIN_SCORE=65`
  only applies when the exploration profile is off. Left unchanged; flagged.
- **13:00** Re-analysed Monday's "blockers" with the correct key
  (`promotion_blockers`, not `reasons`). Finding: **Mon 2026-09-07 was Labor
  Day — US markets were closed all day.** 72 of 72 promotion attempts carried
  `quote_too_old`; AAPL's recorded spread was an identical 1023.6 bps at 17:09,
  19:28 and 12:57 the next day, i.e. one frozen Friday quote. Monday's zero
  trades and its entire blocker mix were artifacts of a closed market, not of
  IEX or the strategies. Correction to earlier analysis recorded in ROADMAP.
  Consequence: the fixed system (Sep 5 fixes) has **never yet seen an open
  market**. Tue 13:30 UTC open is the first real test. No gate changes made.
- **13:02** Railway `watchPatterns` set to `["/**", "!/**/*.md", "!/docs/**"]`
  so that log/roadmap-only pushes no longer restart the bot. Verified after this
  push: no new deployment should appear.
- **15:02** First auto-execution attempt: AMD `anchored_vwap_pullback_continuation`,
  $500 notional. **Blocked at the broker step:** `one_share_exceeds_max_trade_amount`
  (AMD ≈ $501/share > `MAX_TRADE_AMOUNT_USD=500`). Not a gate; a sizing cap.
  Operator decision: raise the per-trade cap or allow fractional shares.
- **17:00:50 — FIRST AUTONOMOUS PAPER TRADE.** GOOGL buy 1 share @ $338.75,
  strategy `opening_range_breakout_retest` (supervised weak-valid path), $500
  notional request, Alpaca paper bracket order `c3bd9790…`: parent filled, stop
  leg $333.24 (held), take-profit leg $349.39 (limit, new). Quote verified live
  (Alpaca IEX), bars fresh. Alpaca reconciliation clean: `positions_seen: 1`,
  `orders_seen: 28`, equity $100,064.13 (baseline $100,064.91), cash $99,726.16.
  Task #56 (autonomous paper-trade fluency) marked complete.
- **13:30–18:30** Live-session stats: 690 scan decisions, 51 near-miss promotion
  attempts, 19 promoted to candidate, 2 reached execution (1 filled, 1 blocked
  by the sizing cap). `workflow_cadence` hit the 240s job timeout 16 times
  (~every 20 min) — the scan still completes enough to trade, but this is the
  recurring item the operator said to leave for now.
- **13:10** Two Railway `redeploy`s of older snapshots appeared (commits
  `c6cd480` and `0d412fb`), not triggered by this session; the surviving
  deployment `ac733f02` runs `c6cd480`, functionally identical to head for the
  app (differs only in CI workflow + a test ceiling). Bot healthy through it.
- **18:32** Re-armed pre-close check routine for 19:30 UTC.
- **18:34** Operator sign-off: `MAX_TRADE_AMOUNT_USD` raised 500 → 1000 (Railway var;
  redeploy triggered — verified below). Caveat found while applying it: the broker
  step sizes as `floor(min(request_amount, max_cap) / price)` and the request
  amount is `DEFAULT_TRADE_AMOUNT_USD=500`, so a $501 AMD share still rounds to 0
  shares. The cap is no longer binding; the default request amount is. Raising
  the default to 1000 doubles every trade's size — left for operator decision.
- **18:37** Operator sign-off: `DEFAULT_TRADE_AMOUNT_USD` raised 500 → 1000 as well
  (Railway var). Every new proposal now requests $1,000 notional (1 share of
  anything up to $1,000). Observation: the 18:34 `MAX_TRADE_AMOUNT_USD` change did
  not produce a deployment on its own; the 18:37 change did (deployment
  `3a2a07f3`, carrying both values). Same failure shape as Mon 19:38 — variable
  changes are unreliable deploy triggers on this service; always verify.
- **18:40** Shipped `4b52d95`: the startup `execution_policy_effective` log now
  includes `default_trade_amount_usd`, `max_trade_amount_usd`, `max_open_positions`,
  `max_trades_per_day`, so sizing-blocked trades are diagnosable from run_logs.
- **18:45** Railway SKIPPED the `7b6ade6` code push: the watch pattern I set at 13:02
  (`/**`) was malformed (gitignore-style needs `**`), so nothing matched the include
  rule and every push was skipped. Fixed to `["**", "!**/*.md", "!/docs/**"]` and
  moved it into `railway.json` (`build.watchPatterns`) so it is version-controlled.
  This push carries both the pattern fix and the policy-snapshot change.
- **18:44 — VERIFIED from the new container's startup log** (deployment `27b1caad`,
  commit `661106a`): `default_trade_amount_usd=1000`, `max_trade_amount_usd=1000`,
  `max_open_positions=3`, `max_trades_per_day=6`, `execution_mode=paper`,
  `enable_real_trading=false`. GOOGL position and bracket unaffected (held at the
  broker). Notion mirror updated with the same entry.
- **18:50** Found the P&L ledger gap: with `paper_broker=alpaca` the coordinator writes
  executions + broker order snapshots but never `paper_positions`/`paper_trades`, so
  the equity curve, EOD digest and strategy scorecard were blind to real broker-backed
  paper trades. Shipped `d8623cc` (`app/paper/broker_ledger.py`): a filled parent
  bracket opens a ledger position from the broker fill; a filled stop/target leg (or
  matched close order) closes it into a paper trade with realized P&L; broker-backed
  rows are only marked-to-market, never closed by the simulator. Idempotent, paper-only.
  Also fixed `test_railway_deployment` for the version-controlled watch patterns.
- **18:58 — VERIFIED in prod** (deployment `03df1d2b`): first refresh wrote GOOGL as an
  open ledger position (entry $338.75, marked $337.86, unrealized −$0.89, stop/target
  from the bracket) and backfilled two old supervised Alpaca tests (AAPL Jul 16 −$1.49,
  NVDA Jun 22 −$0.12) as closed trades. Task #62 complete.
- **19:00** Root cause of "why only GOOGL": 17 of 19 promoted candidates failed at the
  proposal step with `Instrument X is not in the allowed instrument list` (INTC ×6,
  META ×3, AVGO ×2, QQQ ×2, AMZN, TSLA, NFLX, CSCO). `ALLOWED_INSTRUMENTS` was an old
  whitelist (default `NVDA,GOOG,GOOGL,AMD,MU,GOLD`) never widened when the universe was
  trimmed to 25 names — the scanner and the proposal gate disagreed. GOOGL and AMD
  only got through because they were on the old list.
- **19:02** Operator sign-off ("set the allowed instruments to match the universe"):
  `ALLOWED_INSTRUMENTS` set to the exact 25-symbol `MARKET_UNIVERSE_SYMBOLS` value.
  Redeploy verification recorded below.
- **19:07 — VERIFIED.** Allowlist deploy `26356326` booted 19:04:58; follow-up deploy
  `2400459b` (commit `e1a8e9b`, SUCCESS 19:07:45) adds allowlist-vs-universe visibility
  to the startup policy log, which now reads `allowed_instruments_count=25`,
  `market_universe_symbols_count=25`, `universe_not_in_allowlist=[]`. Zero allowlist
  rejections since the fix. Every symbol the scanner promotes can now be proposed.
- **19:10** Operator dissatisfied with pace and asked for a plan to reach "20% every
  day". Answer on record: 20%/day is not achievable by any strategy (compounds $100k to
  $3.8M in a month) and would only be reached by ruinous leverage; declined to build
  toward it. Proposed ladder instead: (1) two weeks of throughput + measurement (5–10
  paper trades/day, 50 closed trades), (2) concentrate on positive-expectancy strategies
  and target 0.1–0.3%/day, (3) scale with capital, not risk. Asked for sign-off on:
  fixing the recurring `workflow_cadence` 240s timeout, risk-based sizing at 0.5% of
  equity per trade, caps 3→5 open positions and 6→12 trades/day, enabling
  auto-demotion after 20 trades per strategy, daily EOD report. Hard gates unchanged.
- **19:20 — P0 FINDING while sizing the 'max per day' question.** The walk-forward
  backtest gate has never measured anything: of ~1,208,139 backtest rows since Aug 1
  (20 strategies × 196 symbols, 31,942 out-of-sample summaries), **every single one has
  `number_of_trades = 0`.** Each fold evaluates ~10 daily bars (`bars_evaluated` avg
  10.2, `fold_count` 37), far too short for any of these strategies to trigger. So there
  is no measured expectancy for any strategy, the "backtest validated" flag has never
  been earned, and the scheduler's 180s backtest budget has been producing empty rows
  for weeks. Task #63 opened: fix fold sizing, assert >0 trades on a trending fixture,
  re-run for the 25-name universe. This is the top priority — nothing about expected
  daily return can be estimated until it is fixed.
- **19:25 — OPERATOR APPROVED the aggressive paper plan + risk settings.** Applied via
  Railway (deployment `b9b8de52`, SUCCESS 19:31): `MAX_RISK_PER_TRADE_PCT=1.0`,
  `DEFAULT_TRADE_AMOUNT_USD=MAX_TRADE_AMOUNT_USD=12500` (per-position notional cap =
  12.5% of equity, so 8 positions ≤ 100% gross, no margin), `MAX_OPEN_POSITIONS=8`,
  `MAX_TRADES_PER_DAY=15`, `MAX_DAILY_LOSS_USD=3000` (3% hard stop, counts open losses),
  `MAX_WEEKLY_LOSS_USD=8000`, `MAX_CONSECUTIVE_LOSSES_BEFORE_COOLDOWN=4` (was 2, which
  would have halted most days by the second loss), drawdown governor ON (soft 2.5%,
  hard 5%, floor 0.5 = size halves), `AUTO_PROPOSE_RISK_BASED_SIZING=true`.
  Hard gates (spread, reward:risk, bracket, rvol, hours, blacklist) unchanged.
- **19:33** Shipped risk-based sizing for the unattended path (`app/risk/proposal_sizing.py`,
  wired in `auto_propose_candidates`). Finding: auto-proposals always passed a flat
  `default_trade_amount_usd`, so `max_risk_per_trade_pct` never applied to any
  autonomous trade — the $1,000 GOOGL trade risked ~$5. Now: notional = 1% of
  reconciled equity ÷ stop distance, capped at $12,500; falls back to the flat default
  if entry/stop/equity are missing (never blocks a proposal); the sizing record is
  stored in proposal metadata. Startup policy log now includes the full risk profile.
  Plan steps still to do: fix the backtester (#63), 5m/15m timeframes + scan cadence
  fix, auto-demote after 15 trades, daily EOD report.
- **19:36 — VERIFIED** the sizing deploy (`866f9413`) from its startup log: risk 1%/trade,
  risk-based sizing on, daily loss $3,000, weekly $8,000, cooldown after 4 losses,
  governor on (floor 0.5), 8 positions, 15 trades/day, paper-only.
- **19:45** Shipped the backtester fix (Task #63). Root cause: each walk-forward fold ran
  the engine on its ~10-bar test slice alone, below every strategy's indicator warm-up.
  Fix: the engine gained `trade_window_start` (warm-up bars are context only — no
  signals, entries or equity points), and folds/holdout now pass train+test bars with
  the test start as the window. Regression test proves the same synthetic folds go from
  0 trades (old) to >0 (new) with every entry inside the test window. 648 tests pass.
  From the next `backtest_gate_refresh` run the gate starts filling with real
  out-of-sample numbers per strategy × symbol.
- **19:41 — VERIFIED** the backtester-fix deploy (`84440f26`) booted (policy log 19:40:58).
- **19:42** `BACKTEST_SCHEDULER_INTERVAL_SECONDS` 21600 → 1800 (Railway var). At ~1 symbol
  per 180s run, the 25-name universe scores overnight (~12h) instead of ~6 days. The
  interval can go back to 6h once the gate is populated.
- **19:44 — VERIFIED** interval deploy (`6210f3db`) booted 19:43:31. First refresh with the
  warm-up fix produced the first non-zero backtest rows since August (34 of 162 rows had
  trades) — the fold fix works. Task #63 closed.
- **19:50 — SECOND ENGINE BUG found in those rows and fixed.** Every fold with one trade
  reported ≈ −82% return even when the trade was profitable: `_close_trade` returns the
  *position's* net proceeds and both call sites assigned it to `cash` instead of adding
  it, discarding the uninvested balance (≈83% of the account at 1% risk sizing). Hidden
  until now because folds never traded and in-sample runs sized all-in. Fix: `cash +=
  realized` at both close sites; regression test asserts ending cash = initial + Σ pnl
  for risk-sized trades. The ~160 rows written 19:40–19:50 carry the wrong returns; the
  30-min refresh overwrites each symbol's summary as it re-runs, and the gate reads the
  latest summary.
- **13:03** Created this file at operator request ("update all the actions you
  are doing"): chose a repo Markdown ledger over Notion because it is
  version-controlled, reviewed by the PR bots, and lives with the code.

## 2026-09-07 (Mon — Labor Day, market closed)

- **~16:50** Redeployed hardened image after the 2-day DB-pool outage (see
  ROADMAP post-mortem). Bot healthy: 0 scheduler errors, paper-safe.
- Railway reliability config set via connector: restart policy `ALWAYS` (10
  retries), healthcheck `/health/ready` (300s).
- Review-team bots fixed (PR #31 opened, secret name matched, token re-pasted
  without newline, `--max-turns 60`). PR #31 CI green and squash-merged to
  `main` (`ffeeda95`).
- DB durable fix shipped: `statement_timeout` (30s) + TCP keepalives in
  `connect_args` (`f6d0d08`); architecture ratchet ceiling for `db.py` bumped
  to 1063 (`0a12efe`).
- **~19:30** Investigated `quote_too_old`. Rejected "re-fetch quote before the
  promotion check" as dead code (quote is fetched immediately before the check
  in `service_scan.py`). Applied `MARKET_UNIVERSE_SYMBOLS` = 25 IEX-liquid names
  via Railway variable. (Retrospective: the staleness that day was the holiday,
  not IEX; the universe trim still stands as harmless and focused.)
- **19:38** The variable change's redeploy stopped the container with no
  replacement → bot down until Tue 12:51. Market had closed 20:00 (and was
  closed all day for the holiday), so no session was lost.
- Published ops dashboard artifact "AlgoTrader Ops".

## Open operator items

- Switch `DATABASE_URL` to the Supabase transaction pooler (port 6543); needs
  the DB password (redacted from the agent).
- Rotate the Claude OAuth token that was pasted into chat once; optionally
  rename the GitHub secret to `CLAUDE_CODE_OAUTH_TOKEN` and revert the workflow
  refs.
- Decide on a paid SIP/NBBO feed (removes IEX single-venue artifacts).
- Fix the `PAPER_EXPLORATION_AUTO_EXECUTION_MIN_SCORE=0.15` typo to an
  intentional value once there is live data to calibrate against.
