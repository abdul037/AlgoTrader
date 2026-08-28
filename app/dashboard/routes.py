"""Read-only operator dashboard: live status, action log, trades, positions.

Serves a self-contained HTML page at ``GET /dashboard`` that polls
``GET /dashboard/data`` for a consolidated JSON snapshot. Everything is
read-only (no control-token needed, no mutations) so it is safe to leave open.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.storage.repositories import ExecutionRepository, RuntimeStateRepository
from app.utils.time import utc_now

router = APIRouter(tags=["dashboard"])


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
        trades.append(
            {
                "created_at": getattr(record, "created_at", None),
                "symbol": req.get("symbol"),
                "side": req.get("side"),
                "amount_usd": req.get("amount_usd"),
                "qty": req.get("qty") or req.get("quantity"),
                "strategy_name": req.get("strategy_name"),
                "mode": getattr(record, "mode", None),
                "status": getattr(record, "status", None),
                "broker_order_id": getattr(record, "broker_order_id", None),
                "realized_pnl_usd": getattr(record, "realized_pnl_usd", 0.0),
                "error_message": getattr(record, "error_message", None),
            }
        )

    proposals = _safe(lambda: _model_list(state.proposal_service.list_proposals()), [])
    scans = _safe(lambda: _model_list(state.scan_decision_repository.list(limit=30)), [])
    positions = _safe(lambda: state.broker_position_repository.list(limit=50), [])

    payload = {
        "generated_at": utc_now().isoformat(),
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
    }
    return JSONResponse(payload)


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
</header>
<main>
  <div class="grid" id="tiles"></div>

  <section>
    <h2>Trade log (executions)</h2>
    <div class="scroll"><table id="trades"><thead><tr>
      <th>Time</th><th>Symbol</th><th>Side</th><th>Qty / $</th><th>Strategy</th>
      <th>Mode</th><th>Status</th><th class="num">Realized P&L</th><th>Broker order</th>
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

  const rec = d.reconciliation || {};
  $('#tiles').innerHTML = [
    ['Account', c.alpaca_account||'—'],
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
    `<td class="muted">${esc((x.broker_order_id||'').slice(0,8))}</td>`
  ]), 9, 'No executions yet.');

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
}
tick(); setInterval(tick, 10000);
</script>
</body>
</html>
"""
