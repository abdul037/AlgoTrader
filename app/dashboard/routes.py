"""Read-only operator dashboard: live status, action log, trades, positions.

Serves a self-contained HTML page at ``GET /dashboard`` that polls
``GET /dashboard/data`` for a consolidated JSON snapshot. Everything is
read-only (no control-token needed, no mutations) so it is safe to leave open.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.performance.stage3 import assess_stage3, real_capital_preflight
from app.performance.strategy_performance import analyze_by_strategy, daily_pnl_series, decay_verdict
from app.storage.repositories import ExecutionRepository, RuntimeStateRepository
from app.utils.time import utc_now

router = APIRouter(tags=["dashboard"])


def _build_info() -> dict[str, Any]:
    """Which commit/build is actually running (Railway injects these env vars)."""

    return {
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        "branch": os.environ.get("RAILWAY_GIT_BRANCH", "unknown"),
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", "unknown"),
        "served_at": utc_now().isoformat(),
    }


@router.get("/version")
def version() -> JSONResponse:
    """Public build marker so the deployed commit can be verified at a glance."""

    return JSONResponse(_build_info())


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - the dashboard must never 500 on one bad section
        return default


def _model_list(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if hasattr(item, "model_dump"):
            out.append(item.model_dump())
        elif isinstance(item, dict):
            out.append(item)
    return out


@router.get("/dashboard/data")
def dashboard_data(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    db = state.db

    worker = getattr(state, "scheduler_worker", None)
    worker_status = _safe(lambda: worker.status(), None) if worker is not None else None

    automation = _safe(lambda: state.automation_service.status().model_dump(), {})
    reconciliation = _safe(lambda: state.safety_state_repository.latest_reconciliation(), None)

    runtime = RuntimeStateRepository(db)
    flags = _safe(
        lambda: runtime.get_many(
            [
                "automation:kill_switch",
                "automation:paused",
                "automation:reason",
                "scheduler_worker:restart_reason",
                "scheduler_worker:tick_count",
            ]
        ),
        {},
    )

    executions = _safe(lambda: ExecutionRepository(db).list(limit=25), [])
    trades: list[dict[str, Any]] = []
    for record in executions:
        req = getattr(record, "request_payload", {}) or {}
        resp = getattr(record, "response_payload", {}) or {}
        trades.append(
            {
                "created_at": getattr(record, "created_at", None),
                "symbol": req.get("symbol"),
                "side": req.get("side"),
                "amount_usd": req.get("amount_usd"),
                "qty": req.get("qty") or req.get("quantity"),
                # Prefer the first-class column; fall back to the payload for rows
                # written before the executions.strategy_name column existed.
                "strategy_name": getattr(record, "strategy_name", None) or req.get("strategy_name"),
                "mode": getattr(record, "mode", None),
                "status": getattr(record, "status", None),
                "broker_order_id": getattr(record, "broker_order_id", None),
                "realized_pnl_usd": getattr(record, "realized_pnl_usd", 0.0),
                "slippage_bps": resp.get("slippage_bps"),
                "error_message": getattr(record, "error_message", None),
            }
        )
    fills_with_slip = [t["slippage_bps"] for t in trades if t.get("slippage_bps") is not None]
    avg_slippage_bps = round(sum(fills_with_slip) / len(fills_with_slip), 2) if fills_with_slip else None

    proposals = _safe(lambda: _model_list(state.proposal_service.list_proposals()), [])
    scans = _safe(lambda: _model_list(state.scan_decision_repository.list(limit=30)), [])
    positions = _safe(lambda: state.broker_position_repository.list(limit=50), [])

    # Stage 1 measurement: per-strategy live performance + live-vs-backtest decay.
    strategy_performance = _safe(lambda: _strategy_performance(state), [])
    pnl_series = _safe(
        lambda: daily_pnl_series(state.paper_trade_repository.list(limit=2000)), []
    )
    stage3 = _safe(lambda: _stage3(state), None)

    payload = {
        "generated_at": utc_now().isoformat(),
        "build": _build_info(),
        "config": {
            "deployment_stage": getattr(settings, "deployment_stage", None),
            "execution_mode": getattr(settings, "execution_mode", None),
            "paper_auto_operation_mode": getattr(settings, "paper_auto_operation_mode", None),
            "enable_real_trading": getattr(settings, "enable_real_trading", None),
            "alpaca_account": getattr(settings, "alpaca_expected_account_number", None),
            "market_data_provider": getattr(settings, "primary_market_data_provider", None),
        },
        "worker": worker_status,
        "automation": automation,
        "flags": flags,
        "reconciliation": reconciliation,
        "trades": trades,
        "proposals": proposals[:25],
        "scans": scans,
        "positions": positions,
        "strategy_performance": strategy_performance,
        "pnl_series": pnl_series,
        "avg_slippage_bps": avg_slippage_bps,
        "stage3": stage3,
    }
    return JSONResponse(payload)


def _stage3(state: Any) -> dict[str, Any] | None:
    """Stage 3 readiness + capital plan ('path to $1k/day') from the paper track record."""

    trade_repo = getattr(state, "paper_trade_repository", None)
    if trade_repo is None:
        return None
    settings = state.settings
    capital = float(getattr(settings, "paper_account_balance_usd", 100_000.0) or 100_000.0)
    target = float(getattr(settings, "weekly_profit_target_usd", 1000.0) or 1000.0)
    # weekly target is a weekly figure elsewhere; here we want the daily goal.
    daily_target = float(getattr(settings, "daily_profit_target_usd", 1000.0) or 1000.0)
    readiness = assess_stage3(
        trade_repo.list(limit=2000), capital_usd=capital, daily_target_usd=daily_target
    )
    preflight = real_capital_preflight(
        readiness=readiness,
        enable_real_trading=bool(getattr(settings, "enable_real_trading", False)),
    )
    plan = readiness.capital_plan
    return {
        "capital_usd": capital,
        "daily_target_usd": daily_target,
        "trading_days": readiness.trading_days,
        "total_trades": readiness.total_trades,
        "realized_pnl_usd": readiness.realized_pnl_usd,
        "sharpe": readiness.sharpe,
        "max_drawdown_pct": readiness.max_drawdown_pct,
        "measured_daily_return_pct": plan.measured_daily_return_pct if plan else 0.0,
        "capital_required_usd": plan.capital_required_usd if plan else None,
        "implied_annualized_return_pct": plan.implied_annualized_return_pct if plan else 0.0,
        "feasibility": plan.feasibility if plan else "no_edge",
        "capital_note": plan.note if plan else "",
        "gates": readiness.gates,
        "blockers": readiness.blockers,
        "stage3_ready": readiness.ready,
        "real_trading_enabled": preflight.real_trading_currently_enabled,
        "decision_allowed": preflight.decision_allowed,
    }


def _strategy_performance(state: Any) -> list[dict[str, Any]]:
    """Per-strategy live paper performance with a keep/watch/demote decay verdict."""

    trade_repo = getattr(state, "paper_trade_repository", None)
    if trade_repo is None:
        return []
    trades = trade_repo.list(limit=2000)
    performances = analyze_by_strategy(trades)
    baseline = _safe(lambda: state.backtest_repository.expectancy_by_strategy(), {})
    min_trades = int(getattr(state.settings, "stage1_decay_min_trades", 20) or 20)
    rows: list[dict[str, Any]] = []
    for perf in performances:
        verdict = decay_verdict(
            perf,
            backtest_expectancy_usd=baseline.get(perf.strategy_name),
            min_trades=min_trades,
        )
        rows.append(
            {
                "strategy_name": perf.strategy_name,
                "trades": perf.trades,
                "win_rate": perf.win_rate,
                "expectancy_usd": perf.expectancy_usd,
                "profit_factor": perf.profit_factor,
                "realized_pnl_usd": perf.realized_pnl_usd,
                "average_r_multiple": perf.average_r_multiple,
                "max_drawdown_usd": perf.max_drawdown_usd,
                "backtest_expectancy_usd": verdict.backtest_expectancy_usd,
                "retention_ratio": verdict.retention_ratio,
                "status": verdict.status,
                "action": verdict.action,
                "reasons": verdict.reasons,
            }
        )
    return rows


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AlgoTrader — Live</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0b0e13; color: #d7dee8;
         font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  header { padding: 14px 20px; border-bottom: 1px solid #1c2530; display: flex;
           align-items: center; gap: 16px; flex-wrap: wrap; position: sticky; top: 0; background: #0b0e13; z-index: 5; }
  h1 { font-size: 16px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  .muted { color: #7c8798; }
  .pill { padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid #26313f; }
  .ok { background: #0f2a1a; color: #57d38c; border-color: #1c5237; }
  .warn { background: #33240c; color: #f0b34a; border-color: #6b4b12; }
  .bad { background: #3a1416; color: #ff6b6b; border-color: #6b1f22; }
  main { padding: 18px 20px 60px; max-width: 1280px; margin: 0 auto; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 22px; }
  .card { background: #10151d; border: 1px solid #1c2530; border-radius: 10px; padding: 12px 14px; }
  .card .k { font-size: 12px; color: #7c8798; margin-bottom: 4px; }
  .card .v { font-size: 18px; font-weight: 650; }
  section { margin-bottom: 26px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .6px; color: #8ea0b6; margin: 0 0 8px; }
  .scroll { overflow-x: auto; border: 1px solid #1c2530; border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #171f29; white-space: nowrap; }
  th { color: #7c8798; font-weight: 600; position: sticky; top: 0; background: #10151d; }
  tr:last-child td { border-bottom: none; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .pos { color: #57d38c; } .neg { color: #ff6b6b; }
  .tag { font-size: 11px; padding: 1px 6px; border-radius: 5px; background: #1a2230; color: #9fb0c6; }
  .empty { padding: 14px; color: #7c8798; }
  .foot { color: #5d6675; font-size: 12px; }
  a { color: #6ea8fe; }
</style>
</head>
<body>
<header>
  <h1>AlgoTrader <span class="muted">— live paper</span></h1>
  <span id="worker" class="pill">worker …</span>
  <span id="autom" class="pill">automation …</span>
  <span id="stage" class="pill">stage …</span>
  <span id="mode" class="pill">—</span>
  <span class="foot" id="updated"></span>
  <span class="foot" id="build"></span>
</header>
<main>
  <div class="grid" id="tiles"></div>

  <section>
    <h2>Equity curve <span class="muted" style="font-weight:400;font-size:12px">(cumulative realized paper P&amp;L)</span></h2>
    <div id="equitywrap" class="scroll"><svg id="equity" width="100%" height="160" preserveAspectRatio="none" viewBox="0 0 1000 160"></svg></div>
    <div id="equityempty" class="empty" style="padding:8px 2px">No closed paper trades yet — the curve draws once Stage 1 trading begins.</div>
  </section>

  <section>
    <h2>Path to $1k/day <span class="muted" style="font-weight:400;font-size:12px">(Stage 3 · capital sizing &amp; readiness)</span></h2>
    <div class="grid" id="stage3tiles"></div>
    <div id="stage3gates" style="margin-top:6px"></div>
    <div id="stage3note" class="muted" style="font-size:12px;margin-top:6px"></div>
  </section>

  <section>
    <h2>Trade log (executions)</h2>
    <div class="scroll"><table id="trades"><thead><tr>
      <th>Time</th><th>Symbol</th><th>Side</th><th>Qty / $</th><th>Strategy</th>
      <th>Mode</th><th>Status</th><th class="num">Realized P&L</th><th class="num">Slip bps</th><th>Broker order</th>
    </tr></thead><tbody></tbody></table></div>
  </section>

  <section>
    <h2>Open positions (Alpaca)</h2>
    <div class="scroll"><table id="positions"><thead><tr>
      <th>Symbol</th><th class="num">Qty</th><th class="num">Avg</th>
      <th class="num">Mkt value</th><th class="num">Unrealized P&L</th>
    </tr></thead><tbody></tbody></table></div>
  </section>

  <section>
    <h2>Proposals</h2>
    <div class="scroll"><table id="proposals"><thead><tr>
      <th>Time</th><th>Symbol</th><th>Side</th><th>Strategy</th><th>Status</th>
    </tr></thead><tbody></tbody></table></div>
  </section>

  <section>
    <h2>Strategy performance <span class="muted" style="font-weight:400;font-size:12px">(live paper · keep / watch / demote)</span></h2>
    <div class="scroll"><table id="stratperf"><thead><tr>
      <th>Strategy</th><th class="num">Trades</th><th class="num">Win%</th>
      <th class="num">Expectancy</th><th class="num">PF</th><th class="num">Realized P&amp;L</th>
      <th class="num">Max DD</th><th class="num">vs BT</th><th>Verdict</th>
    </tr></thead><tbody></tbody></table></div>
  </section>

  <section>
    <h2>Action log (recent scan decisions)</h2>
    <div class="scroll"><table id="scans"><thead><tr>
      <th>Time</th><th>Task</th><th>Symbol</th><th>Strategy</th><th>TF</th>
      <th>Status</th><th class="num">Score</th><th>Why not</th>
    </tr></thead><tbody></tbody></table></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = v => (v==null?'':String(v)).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const t = iso => { if(!iso) return ''; const d=new Date(iso); return isNaN(d)?esc(iso):d.toISOString().replace('T',' ').slice(5,19)+'Z'; };
const num = (v,d=2) => (v==null||v==='')?'':Number(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const pnl = v => { if(v==null||v==='') return ''; const n=Number(v); const c=n>0?'pos':(n<0?'neg':''); return `<span class="${c}">${n>0?'+':''}${num(n)}</span>`; };
function pill(el, text, cls){ el.textContent=text; el.className='pill '+cls; }
function drawStage3(s){
  const tiles = $('#stage3tiles'), gatesEl = $('#stage3gates'), noteEl = $('#stage3note');
  if(!s){ tiles.innerHTML=''; gatesEl.innerHTML=''; noteEl.textContent=''; return; }
  const capReq = s.capital_required_usd==null ? '—' : '$'+num(s.capital_required_usd,0);
  const feasCls = {plausible:'ok', top_decile:'warn', implausible_extrapolation:'bad', no_edge:'bad'}[s.feasibility]||'warn';
  tiles.innerHTML = [
    ['Measured return/day', num(s.measured_daily_return_pct,3)+'%'],
    ['Capital for $'+num(s.daily_target_usd,0)+'/day', capReq],
    ['Implied annual', num(s.implied_annualized_return_pct,0)+'%'],
    ['Track record', (s.trading_days||0)+' days · '+(s.total_trades||0)+' trades'],
    ['Sharpe', num(s.sharpe,2)],
    ['Max DD', num(s.max_drawdown_pct,1)+'%'],
  ].map(([k,v])=>`<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');
  const gate = (label,ok)=>`<span class="pill ${ok?'ok':'bad'}">${ok?'✓':'✗'} ${esc(label)}</span>`;
  const g = s.gates||{};
  gatesEl.innerHTML =
    gate('60+ days', g.track_record_days) + ' ' + gate('100+ trades', g.trade_count) + ' ' +
    gate('Sharpe ≥1.5', g.sharpe) + ' ' + gate('DD ≤8%', g.drawdown) + ' ' +
    gate('positive expectancy', g.positive_expectancy) + ' ' +
    `<span class="pill ${s.stage3_ready?'ok':'warn'}">${s.stage3_ready?'READY for capital decision':'accruing track record'}</span>` +
    (s.real_trading_enabled?` <span class="pill bad">⚠ REAL TRADING ON</span>`:` <span class="pill ok">paper only</span>`);
  noteEl.innerHTML = `<span class="pill ${feasCls}" style="margin-right:6px">${esc(s.feasibility||'')}</span>${esc(s.capital_note||'')}`;
}
function drawEquity(series){
  const svg = $('#equity'), empty = $('#equityempty');
  if(!series || series.length < 2){ svg.style.display='none'; empty.style.display='block'; svg.innerHTML=''; return; }
  svg.style.display='block'; empty.style.display='none';
  const W=1000, H=160, pad=8;
  const vals = series.map(p=>Number(p.cumulative_pnl_usd)||0);
  let lo=Math.min(0,...vals), hi=Math.max(0,...vals); if(hi===lo) hi=lo+1;
  const x=i=>pad + i*(W-2*pad)/(series.length-1);
  const y=v=>H-pad - (v-lo)*(H-2*pad)/(hi-lo);
  const line = vals.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${line} L${x(vals.length-1).toFixed(1)},${y(lo).toFixed(1)} L${x(0).toFixed(1)},${y(lo).toFixed(1)} Z`;
  const up = vals[vals.length-1] >= 0;
  const stroke = up ? '#57d38c' : '#ff6b6b', fill = up ? 'rgba(87,211,140,.14)' : 'rgba(255,107,107,.14)';
  const zeroY = y(0).toFixed(1);
  svg.innerHTML =
    `<line x1="0" y1="${zeroY}" x2="${W}" y2="${zeroY}" stroke="#26313f" stroke-width="1" stroke-dasharray="4 4"/>`+
    `<path d="${area}" fill="${fill}"/>`+
    `<path d="${line}" fill="none" stroke="${stroke}" stroke-width="2"/>`;
}
function rows(tbodySel, data, cols, emptyMsg){
  const tb = $(tbodySel).tBodies[0];
  if(!data || !data.length){ tb.innerHTML=`<tr><td class="empty" colspan="${cols}">${emptyMsg}</td></tr>`; return; }
  tb.innerHTML = data.map(r=>'<tr>'+r.map(c=>c).join('')+'</tr>').join('');
}
async function tick(){
  let d; try { d = await (await fetch('/dashboard/data',{cache:'no-store'})).json(); }
  catch(e){ $('#updated').textContent='(offline — retrying)'; return; }
  const w = d.worker || {}, a = d.automation || {}, f = d.flags || {}, c = d.config || {};
  const age = w.heartbeat_age_seconds;
  if(w.running && age!=null && age<60) pill($('#worker'), `worker ♥ ${Math.round(age)}s`, 'ok');
  else if(w.running) pill($('#worker'), `worker slow ${age==null?'?':Math.round(age)+'s'}`, 'warn');
  else pill($('#worker'), 'worker down', 'bad');
  const paused = String(f['automation:paused']).toLowerCase()==='true';
  const kill = String(f['automation:kill_switch']).toLowerCase()==='true';
  if(kill) pill($('#autom'),'kill-switch ON','bad');
  else if(paused) pill($('#autom'),'paused','warn');
  else pill($('#autom'),'automation live','ok');
  pill($('#stage'), 'stage: '+(c.deployment_stage||'?'), 'warn');
  const real = c.enable_real_trading===true || String(c.enable_real_trading).toLowerCase()==='true';
  pill($('#mode'), (c.execution_mode||'?')+(real?' ⚠ REAL':' · paper'), real?'bad':'ok');
  $('#updated').textContent = 'updated '+t(d.generated_at);
  const b = d.build || {};
  $('#build').textContent = b.commit ? ('build '+String(b.commit).slice(0,7)) : '';

  const rec = d.reconciliation || {};
  const series = d.pnl_series || [];
  const totalPnl = series.length ? series[series.length-1].cumulative_pnl_usd : null;
  const todayStr = new Date().toISOString().slice(0,10);
  const todayRow = series.length ? series[series.length-1] : null;
  const todayPnl = (todayRow && todayRow.date===todayStr) ? todayRow.realized_pnl_usd : 0;
  $('#tiles').innerHTML = [
    ['Account', c.alpaca_account||'—'],
    ['Realized P&L', totalPnl==null?'—':(totalPnl>0?'+':'')+'$'+num(totalPnl,0)],
    ['Today P&L', (todayPnl>0?'+':'')+'$'+num(todayPnl,0)],
    ['Avg slip', d.avg_slippage_bps==null?'—':num(d.avg_slippage_bps,1)+' bps'],
    ['Ticks', w.tick_count!=null?w.tick_count:'—'],
    ['Restarts', (w.restart_count!=null?w.restart_count:'—')+(f['scheduler_worker:restart_reason']?` (${esc(f['scheduler_worker:restart_reason'])})`:'')],
    ['Positions', (d.positions||[]).length],
    ['Recon', (rec.status||'—')+(rec.positions_seen!=null?` · ${rec.positions_seen} pos`:'')],
    ['Data', c.market_data_provider||'—'],
    ['Trades', (d.trades||[]).length],
    ['Proposals', (d.proposals||[]).length],
  ].map(([k,v])=>`<div class="card"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join('');

  rows('#trades', (d.trades||[]).map(x=>[
    `<td>${t(x.created_at)}</td>`,`<td>${esc(x.symbol)}</td>`,`<td>${esc(x.side)}</td>`,
    `<td>${x.qty!=null?esc(x.qty):(x.amount_usd!=null?'$'+num(x.amount_usd,0):'')}</td>`,
    `<td><span class="tag">${esc(x.strategy_name||'')}</span></td>`,`<td>${esc(x.mode)}</td>`,
    `<td>${esc(x.status)}</td>`,`<td class="num">${pnl(x.realized_pnl_usd)}</td>`,
    `<td class="num">${x.slippage_bps==null?'':(x.slippage_bps>0?'+':'')+num(x.slippage_bps,1)}</td>`,
    `<td class="muted">${esc((x.broker_order_id||'').slice(0,8))}</td>`
  ]), 10, 'No executions yet.');

  rows('#positions', (d.positions||[]).map(p=>[
    `<td>${esc(p.symbol)}</td>`,`<td class="num">${num(p.quantity,4)}</td>`,
    `<td class="num">${num(p.average_price)}</td>`,`<td class="num">${num(p.market_value)}</td>`,
    `<td class="num">${pnl(p.unrealized_pnl)}</td>`
  ]), 5, 'Flat — no open positions.');

  rows('#proposals', (d.proposals||[]).map(p=>[
    `<td>${t(p.created_at)}</td>`,`<td>${esc(p.symbol)}</td>`,`<td>${esc(p.side)}</td>`,
    `<td><span class="tag">${esc(p.strategy_name||'')}</span></td>`,`<td>${esc(p.status)}</td>`
  ]), 5, 'No proposals yet.');

  rows('#scans', (d.scans||[]).map(s=>[
    `<td>${t(s.created_at)}</td>`,`<td>${esc(s.scan_task)}</td>`,`<td>${esc(s.symbol)}</td>`,
    `<td><span class="tag">${esc(s.strategy_name||'')}</span></td>`,`<td>${esc(s.timeframe)}</td>`,
    `<td>${esc(s.status)}</td>`,`<td class="num">${s.final_score!=null?num(s.final_score,1):''}</td>`,
    `<td class="muted">${esc((s.rejection_reasons||[]).join(', '))}</td>`
  ]), 8, 'No scans recorded yet (runs during market hours).');

  drawEquity(d.pnl_series || []);
  drawStage3(d.stage3);

  const verdictCls = {healthy:'ok', insufficient_data:'warn', decaying:'warn', dead:'bad'};
  rows('#stratperf', (d.strategy_performance||[]).map(p=>{
    const cls = verdictCls[p.status] || 'warn';
    const label = String(p.action||'').toUpperCase()+' · '+String(p.status||'').replace(/_/g,' ');
    const vsBt = p.retention_ratio!=null ? Math.round(p.retention_ratio*100)+'%' : '—';
    return [
      `<td><span class="tag">${esc(p.strategy_name)}</span></td>`,
      `<td class="num">${p.trades!=null?p.trades:''}</td>`,
      `<td class="num">${num(p.win_rate,1)}</td>`,
      `<td class="num">${pnl(p.expectancy_usd)}</td>`,
      `<td class="num">${num(p.profit_factor,2)}</td>`,
      `<td class="num">${pnl(p.realized_pnl_usd)}</td>`,
      `<td class="num">${p.max_drawdown_usd!=null?'-'+num(p.max_drawdown_usd,0):''}</td>`,
      `<td class="num">${esc(vsBt)}</td>`,
      `<td><span class="pill ${cls}">${esc(label)}</span></td>`
    ];
  }), 9, 'No closed paper trades yet — populates once Stage 1 trading begins.');
}
tick(); setInterval(tick, 10000);
</script>
</body>
</html>
"""
