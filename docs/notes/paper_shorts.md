# Shorts step 3: paper short entries on Alpaca paper (Phase A)

Status: built and tested on `claude/wip-paper-shorts`; NOT deployed. Operator decision 2026-11-16:
step 3 (paper shorts) approved; step 4 (real-money eToro short CFDs) approved for later and is
Phase B, not built here. Practice money only.

## 1. The switch

One new setting, `paper_short_entries_enabled` (default **False**), read through
`app.risk.short_side.paper_shorts_enabled(settings)`, which is True only when ALL hold:

* `paper_short_entries_enabled` is true;
* `execution_mode == "paper"` and `enable_real_trading` is false;
* `paper_broker == "alpaca"` and `broker_for_equities == "alpaca"` (the short is a real Alpaca
  paper order: the borrow and margin facts come from Alpaca's asset data).

Why a new setting and not `short_trading_enabled`:

* a fresh name cannot already be set in Railway, so the deploy itself cannot start shorting;
* `short_trading_enabled` stays the generic, mode-independent switch in `ShortTradePolicy`
  (still false). The paper switch only answers the policy's "is shorting switched on?" question
  for an order that is ALSO tagged paper-only on the paper account; every other short control
  (minimum equity, shortable, easy-to-borrow, borrow cost cap, margin requirement) still applies
  unchanged, and two tightenings were added (below);
* Phase B gets its own eToro-side switch; nothing here can open a real-money short.

Switch off = behaviour identical: every new branch is behind `paper_shorts_enabled`, the short
candidates are dropped at the same place with the same funnel label (`dropped:entry_short`),
the long evidence gate, the pass order and the tags are unchanged (regression tests in
`tests/test_paper_shorts_admission.py`).

## 2. Admission (the auto-propose loop)

The live swing and intraday scans already return bracketed `entry_short` candidates
(`response.candidates`; the screener scores and filters shorts with mirrored rules). Today the
loop drops them (`candidate_propose_drop_reason` -> `entry_short`). With the switch on they are
routed into the same loop; no new signal is invented. The pre-market deep scan's
`short_signals` list stays shadow-only (pre-market, not tradeable).

Evidence for a short is its OWN short verdict, never the long one:
`strategy_evidence:short_verdicts` key `<strategy>:short:<tf>` (`short_research.py`).
`short_evidence_blocker` mirrors the paper gate: admitted when `passed`, or when
`paper_oos_bar_enabled` and `paper_oos_passed`; refused as `strategy_lacks_oos_evidence`
(missing / failing) or `strategy_evidence_stale` (> 24 h); `strategy_retired` first. It always
applies to a short (it does not depend on `require_strategy_oos_evidence`).

(a) **Verdict short** (its short verdict admits it): a normal paper short, sized at the risk
    gate's per-trade budget (`sizing_risk_pct`, forced risk-based), tagged
    `paper_only: true`, `paper_only_reason: "short"`. Because it is paper-only it takes the
    existing paper-only rules of `app/workflow/paper_only.py` (ordered after eToro-eligible
    entries; one a pass; never an eToro-held symbol; leave 2 trades/day and 1 group entry; book
    and heat headroom; automatic only) and shares `MAX_OPEN_PAPER_ONLY` (5) with the OOS-only
    longs. Today only `trend_following:short:1d` qualifies (`paper_oos_passed`, +0.065 R / 138
    OOS trades).
(b) **Exploration short** (refused ONLY for its evidence): goes through the exploration lane
    unchanged and shares its caps with exploration longs: 0.25% risk, 8 open, 12 new per New
    York day, 1 per pass, no intraday entry after 15:30 New York, timeframes 5m/10m/15m/1h/1d,
    equities only, never an eToro-held symbol, every hard gate. Tagged
    `paper_only_reason: "exploration"` plus `position_side: "short"`.

For both paths: the execution check (`candidate_blockers`) no longer adds
`short_entries_disabled`, and the quality label treats a short like a long. The two
long-evidence blockers of `EXECUTION_BYPASS` (`candidate_not_backtest_validated`: the
screener's per-symbol check runs the LONG backtest; `insufficient_strategy_closed_trade_evidence`:
the strategy's closed LONG trades) say nothing about a short and are bypassed for a short as the
lane bypasses them; the bypassed list is recorded on the order (`paper_short_bypassed`).
Negative live evidence (`strategy_inactive`, tier-3 expectancy / profit factor), retirement,
governance and every hard gate still refuse a short.

Before any short proposal the pass reads Alpaca's asset and account (once per symbol a pass)
and refuses with `paper_short_*` blockers: not tradable/shortable, hard-to-borrow, not
marginable, no maintenance margin, account shorting disabled, or unreadable.

## 3. Execution on Alpaca paper

* Sell-to-open bracket: market SELL, stop-loss leg a buy-stop ABOVE the entry, take-profit leg a
  buy-limit BELOW it, GTC, whole shares (`AlpacaClient.submit_bracket_order(..., opens_short=True)`;
  a SELL without `opens_short` is still refused by the client).
* The coordinator's bracket check for a SELL requires the order to be a tagged paper short with
  the switch on (else `long_entries_only`, as before), then: stop above the quote, target below
  it, reward:risk at the quote = (quote - target) / (stop - quote).
* Size by risk: shares = risk $ / (stop - entry); the proposal notional is that x entry (the
  same `risk_based_proposal_notional`, which uses |entry - stop|), capped by the per-trade
  notional cap; the coordinator buys floor(notional / quote) whole shares.
* `ShortTradePolicy` passes on real Alpaca asset data, written into the order metadata at
  proposal time and RE-READ by the coordinator just before the risk check:
  `shortable`, `easy_to_borrow`, `marginable`, `margin_requirement_pct` =
  Alpaca's `maintenance_margin_requirement`, account `shorting_enabled`.
* Borrow cost: Alpaca's asset API publishes no borrow rate (`borrow_cost_available: false`) and
  Alpaca only opens shorts in easy-to-borrow names. An ETB name is given the repo's own ETB
  general-collateral assumption, 0.30 %/year (`CostModel.alpaca_equities().short_borrow_fee_daily_pct
  x 360`, the rate the short research already charges), recorded as
  `borrow_cost_source: "cost_model_etb_assumption"`; it is under the 5 % cap. A hard-to-borrow
  name gets no rate and is refused (`short_not_easy_to_borrow` and `short_borrow_cost_missing`).
* Tightenings in `ShortTradePolicy` (no effect while nothing shorts): every SELL entry is a
  short entry (the `opens_short` flag is no longer needed to engage the controls); a short needs
  `shortable` (and not `marginable: false`); a short outside paper mode or with real trading on
  is refused (`short_trading_live_not_enabled`).

## 4. Never on eToro (until Phase B)

* Tag: every short is `paper_only: true` (both paths), so every existing paper-only guard applies
  (mirror refusal, comparison skip, coordinator eToro-symbol re-check, readers).
* Explicit refusal: `EtoroLiveMirrorService.blockers` refuses ANY order that opens a short (a
  SELL entry, or `opens_short` / `position_side: short` metadata) with
  `etoro_live_short_not_enabled` as its first check after the disabled check, reading nothing
  else, tagged or not; `mirror()` re-checks just before the POST; the refusal is logged as
  `etoro_live_short_not_copied` (so shorts never crowd the scorecard's skipped list). Not a room
  reason: paper's own room rules decide a short.

## 5. Lifecycle (long-only assumptions found and handled)

| Place | Assumption | Handling |
| --- | --- | --- |
| `reliability.candidate_propose_drop_reason` | drops `entry_short` | `allow_short` (pass passes the switch) |
| `reliability.proposal_quality_label` | short = not_tradeable | `allow_short` |
| `unattended.candidate_blockers` | `short_entries_disabled` | only while the switch is off |
| `unattended.candidate_proposal_blockers` | long verdict for every candidate | short verdict for a short (switch on) |
| `alpaca_client.submit_bracket_order` | BUY only, stop < target | `opens_short=True`: SELL, stop > target |
| `alpaca_client.get_portfolio` | qty sign | short qty normalised negative from `side` |
| `coordinator._alpaca_bracket_reasons` | `long_entries_only`, stop < quote < target | short rules for tagged paper shorts |
| `reconciliation` standalone exits | filled standalone SELLs only | both sides; the entry's closing side picked |
| `reconciliation._realized_pnl` / standalone P/L | (exit - entry) x qty | signed by the entry side |
| `reconciliation._learning_event_type` | filled SELL leg = closure | closing side |
| `reconciliation` slippage bps | positive = paid up (long) | signed by the entry side |
| reconciliation unprotected check | - | already side-aware (`_closing_side`, live "stop" leg); tested with a buy-stop above |
| `paper/service._broker_execution_record` | exit leg = SELL; separate close only after a BUY; P/L sign | closing side; both entry sides; signed P/L |
| `paper/service._bracket_legs_verified` | SELL legs | closing-side legs |
| `paper/service._next_entry_time` / separate-exit match | next BUY / a SELL close | entry side / closing side |
| `paper/broker_ledger._exit_outcome` / `_exit_filled_at` | SELL leg | closing side |
| `intraday_exit`, `max_hold_exit` | - | side-agnostic (`close_position`); tested with shorts |
| `auto_recover` | - | reads reconciliation only; a protected short leaves it clean (tested) |
| `leverage_sim` | 1x pays no financing, no forced close at 1x | a short (an eToro CFD) pays overnight at 1x and is force-closed at a 100 % adverse move |
| leverage leaderboard / `paper_only_report` | rows keyed by strategy:tf | shorts keyed `<strategy>:short:<tf>` with `side`; verdict shorts in their own `short` lane |
| scorecard page | no side | `side` in paper rows; "short" shown |
| `guardrails` sector cap | `is_buy` only | listed: a new short is not sector-capped; open shorts do count against a new long (conservative) |
| `market_direction_reasons` | buy only | listed: no "rising market" filter for intraday shorts (open question) |
| eToro exit copy | follows paper by symbol | a short never opens on an eToro-held symbol (rule 3 + coordinator re-check) and paper holds one position a symbol |
| self-simulated paper broker | - | already side-aware; not used (switch needs Alpaca) |

## 6. What Phase B (eToro short CFDs) will need

* Its own switch and constants in `etoro_live_mirror.py`, replacing the
  `etoro_live_short_not_enabled` refusal for a short that passes the SAME copy rules as a long:
  `passed` short verdict (not `paper_oos_passed`), +0.03 R bar, 24 h streak on the SHORT key
  (`passing_since` would need adding to the short verdicts payload), the per-signal check, a
  paper record of closed paper SHORTS on that key, the planner, a mirrored no-chase rule
  (fall over 20 days, RSI floor, 252-day low).
* Path (a) shorts would then need to stop being tagged paper-only when they qualify (today the
  tag is unconditional); exploration shorts stay paper-only.
* Copy mechanics: `IsBuy: false` orders, stop ABOVE / target BELOW, stop distance and sizing
  from (stop - entry), the exposure cap counting short notional, the backup stop and overnight
  alert mirrored (a rise towards the stop), the exit copy matching side, the scorecard's R sign.
* Costs: eToro short CFD overnight fee at 1x (the leverage sim already models it), no borrow.
* Room: eToro's bucket / open caps counting shorts; whether a short may hedge a long group.

## 7. Open questions for the operator

1. Cap for verdict shorts: they share the OOS-only paper-only cap (5 open). Separate cap?
2. Verdict shorts bypass the two long-evidence blockers (per-symbol LONG backtest validation,
   30 closed LONG trades for tier 3). Keep, or require a per-symbol short check?
3. Negative live evidence of the strategy's LONGS (`strategy_inactive`, tier-3 expectancy) also
   refuses its shorts. Keep (conservative) or judge shorts on their own record?
4. Intraday shorts have no market-direction filter (longs are blocked while SPY falls). Add the
   mirror image (no intraday shorts while SPY is above VWAP and up >= 0.3 %)?
5. The sector cap ignores a new short. Count shorts per sector separately?
6. Borrow cost: 0.30 %/yr ETB assumption (not 0). OK?

## 8. Tests

See the test files named `tests/test_paper_shorts_*.py` and the results recorded in the final
report.
